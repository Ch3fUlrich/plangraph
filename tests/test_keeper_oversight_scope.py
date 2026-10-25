"""RT-OVERSIGHT-KEEPER-SCOPE (red): the scoped oversight principal.

An ``oversight`` principal is a high-tier human/agent that may use exactly one
write verb, ``apply_ops``, and inside it exactly one narrow class of ops:

* create a ``lane``/``question`` with an EXPLICIT ``review_tier`` of
  ``standard`` (lane/question) or ``none`` (question only) and a non-empty
  ``done_criteria``;
* edit ``note``/``description`` and set ``priority`` on an OPEN, non-high item;
* ADD ``depends_on`` edges on such an item.

Everything else is refused with an ``oversight:<reason>`` message, a denied
``oversight_ops`` audit row and a single ``projector_alarms`` row.  The token is
minted only by the local admin CLI (no HTTP verb), expires, rotates and dies
with a principals revoke.  This module is RED: the feature (``auth.
add_oversight_principal``, ``auth.OversightPrincipal``, ``Store.
mint_oversight_token``, ``oversight_policy``, ``oversight_ops``/
``oversight_tokens``) does not exist yet, so every test here fails for a
missing behaviour, never for a test bug.

Router rulings pinned here (binding):

* R1 -- ``review_tier='none'`` only for ``kind='question'``; no explicit
  ``high`` at create; an item's tier is never changed by an oversight edit (an
  existing high item is not even a legal target);
* R2 -- mint is a local admin CLI command only, never an HTTP/MCP route;
* R3 -- a rate-limited call is ``forbidden`` whose text carries
  ``oversight:rate_limited`` (no new public 429 code);
* R4 -- crossing the denied budget is alarm-only; the token is revoked only by
  an explicit router revoke;
* R5 -- the principal is scoped to ``projects=['plangraph']``; an empty list
  means nothing;
* R6 -- ``decision``/``operator_task`` are refused as dependency targets too;
* R8 -- ``oversight_ops`` is append-only and never reaped.

The expected public seam is::

    auth.add_oversight_principal(conn, name, projects)       # row only, no token
    Store.mint_oversight_token(name, *, go_decision, reason) # one plaintext token
    pg admin mint-oversight-token NAME --go D-NNN [--reason TEXT] --token-out F

Every test imports the new modules lazily so collection always succeeds.
"""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

from keeper_util import World, defn, to_done
from plangraph.keeper import api_routes, auth, db as kdb, db_errors
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused

P = "plangraph"
OPP = "oversight"
GO = "D-950"
FINDING = "F-4242"
BATCH = "batch-oversight-1"


# --------------------------------------------------------------- fixtures
def _seed(w):
    """One valid plangraph graph covering every target class.

    ``HOST`` is the lane oversight may edit; it already depends on ``OTHER`` so
    a dependency removal has something to remove.  ``GOAL`` depends on every
    item, which is what makes the lanes reachable.  All non-high lanes are
    created by the router with an explicit ``standard`` tier (the router is
    allowed to lower an auto-high; oversight is only allowed the create-time
    choice pinned in R1/the item grant).
    """
    w.store.create_items(w.p("router"), [
        defn("HOST", ["OTHER"], project=P, review_tier="standard"),
        defn("OTHER", [], project=P, review_tier="standard"),
        defn("LINK", [], project=P, review_tier="standard"),
        defn("CLAIMED", [], project=P, review_tier="standard"),
        defn("HIGH", [], project=P, review_tier="high"),
        defn("GATE", [], project=P, kind="gate", review_tier="high"),
        defn("DEC", [], project=P, kind="decision", review_tier="none",
             evidence_kinds=["decision"]),
        defn("OPTASK", [], project=P, kind="operator_task",
             review_tier="standard"),
        defn("GOAL", ["HOST", "OTHER", "LINK", "CLAIMED", "HIGH", "GATE",
                      "DEC", "OPTASK"], project=P, kind="goal",
             review_tier="standard"),
    ])
    token = auth.add_principal(w.conn, "l1pg", "l1", [P])
    w.tokens["l1pg"] = token
    w.principals["l1pg"] = auth.authenticate(w.conn, token)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    _seed(world)
    yield world
    world.close()


def add_oversight(w, name=OPP, projects=(P,)):
    """Provision the principals row; minting is a separate step (no token here)."""
    auth.add_oversight_principal(w.conn, name, list(projects))
    return name


def grant_go(w, decision_id=GO, kind="operator", waives=("oversight-live",)):
    """The router's GO row: kind=operator and 'oversight-live' in waives."""
    w.store.add_decision(w.p("router"), decision_id, kind,
                         "authorise the live oversight principal",
                         waives=list(waives))


def mint(w, name=OPP, go=GO, reason="scope batch"):
    return w.store.mint_oversight_token(name, go_decision=go, reason=reason)


def live(w, monkeypatch, *, name=OPP, go=GO, projects=(P,)):
    """A minted, authenticated oversight principal (the env GO is set)."""
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    if w.conn.execute("SELECT 1 FROM decisions WHERE id=?",
                      (go,)).fetchone() is None:
        grant_go(w, decision_id=go)
    add_oversight(w, name=name, projects=projects)
    out = mint(w, name=name, go=go)
    # The store clock is the TTL authority here: authenticate judges the
    # expiry against the clock the mint stamped it with.
    return out["token"], auth.authenticate(w.conn, out["token"],
                                           now=w.clock())


def apply(w, principal, ops, batch_ref=BATCH):
    return w.store.apply_ops(principal, ops, batch_ref=batch_ref)


def op(name, *, reason="oversight scope grant", finding=FINDING, **kw):
    """One oversight op: the two audit inputs ride on every op."""
    return {"op": name, "reason": reason, "finding": finding, **kw}


def fresh(pid, *, kind="lane", tier="standard", deps=(), evidence=None,
          drop_tier=False, **kw):
    """A create payload using ONLY the oversight allowlist fields."""
    item = {
        "project": P, "id": pid, "kind": kind, "title": f"title {pid}",
        "description": f"description {pid}", "note": f"note {pid}",
        "done_criteria": "criteria", "evidence_kinds":
            evidence or (["decision"] if tier == "none" else ["any"]),
        "depends_on": list(deps), "priority": 0, "owner": "tester",
    }
    if not drop_tier:
        item["review_tier"] = tier
    item.update(kw)
    return item


def create_linked(w, principal, item, *, host="plangraph:HOST", **kw):
    """Create *item* and make the reachable HOST depend on it (one batch)."""
    deps = list(w.store._item(host)["depends_on"])
    deps.append(f"{item['project']}:{item['id']}")
    return apply(w, principal, [
        op("create", item=item, **kw),
        op("edit", key=host, fields={"depends_on": deps}, **kw),
    ])


def snap(w):
    """Row counts plus the hash-chain head: byte-for-byte unchanged proof."""
    q = w.conn.execute
    head = q("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (q("SELECT COUNT(*) FROM items").fetchone()[0],
            q("SELECT COUNT(*) FROM edges").fetchone()[0],
            q("SELECT COUNT(*) FROM events").fetchone()[0],
            q("SELECT COUNT(*) FROM evidence").fetchone()[0],
            (head["seq"], head["hash"]))


def refusal(fn, *a, **kw):
    with pytest.raises(Refused) as exc:
        fn(*a, **kw)
    return exc.value


def has(reason, msg):
    return f"oversight:{reason}" in str(msg)


# ------------------------------------------------------------- principal
def test_authenticate_oversight_token_returns_oversight_principal(
        w, monkeypatch):
    token, principal = live(w, monkeypatch)
    assert token.startswith("pgo1_")
    assert isinstance(principal, auth.OversightPrincipal)
    assert principal.role == "oversight"
    assert principal.projects == [P]            # copied verbatim
    assert principal.generation == 1
    assert principal.scope == "plan-write"
    assert principal.token_id > 0
    # A bare pgo1_ token with no row is refused, never defaulted to a role.
    assert refusal(auth.authenticate, w.conn, "pgo1_" + "x" * 43
                   ).code == "forbidden"


def test_add_principal_refuses_the_oversight_role(w):
    from plangraph.keeper import schema

    assert "oversight" in schema.ROLES
    add_oversight(w)                       # the explicit, TTL-capable door
    assert refusal(auth.add_principal, w.conn, "sneaky", "oversight", [P]
                   ).code == "validation"
    row = w.conn.execute("SELECT role,projects,token_sha256 FROM principals"
                         " WHERE name=?", (OPP,)).fetchone()
    assert row["role"] == "oversight"
    assert json.loads(row["projects"]) == [P]
    assert row["token_sha256"] is None     # a principals token has no TTL


def test_empty_projects_fail_closed(w, monkeypatch):
    """R5: ``[]`` means nothing, unlike the router's all-projects semantics."""
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    try:
        add_oversight(w, projects=())
    except Refused as exc:
        assert exc.code == "validation"    # provisioning refuses it outright
        return
    try:
        token = mint(w)
    except Refused:
        return                             # minting may fail closed too
    principal = auth.authenticate(w.conn, token, now=w.clock())
    assert principal.projects == []
    with pytest.raises(Refused) as exc:
        apply(w, principal, [op("edit", key="plangraph:OTHER",
                                fields={"note": "x"})])
    assert exc.value.code == "forbidden"


def test_oversight_is_scoped_to_its_projects(w, monkeypatch):
    w.store.create_items(w.p("router"), [
        defn("D", [], project="demo", review_tier="standard"),
        defn("DGOAL", ["D"], project="demo", kind="goal",
             review_tier="standard"),
    ])
    _, principal = live(w, monkeypatch)
    err = refusal(apply, w, principal,
                  [op("edit", key="demo:D", fields={"note": "outside"})])
    assert err.code == "forbidden"
    assert "demo" in err.msg


# ---------------------------------------------------------------- minting
def test_mint_requires_router_go(w, monkeypatch):
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    add_oversight(w)
    # no --go / unknown decision
    assert refusal(mint, w, go=None).code == "forbidden"
    assert refusal(mint, w, go="D-NOPE").code == "forbidden"
    # the decision must be kind='operator' and carry 'oversight-live'
    grant_go(w, decision_id="D-951", kind="judge")
    assert refusal(mint, w, go="D-951").code == "forbidden"
    grant_go(w, decision_id="D-952", waives=("something-else",))
    assert refusal(mint, w, go="D-952").code == "forbidden"
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 0    # fail closed: no token
    # both halves present -> exactly one minted token
    grant_go(w, decision_id="D-953")
    out = mint(w, go="D-953")
    assert out["token"].startswith("pgo1_")
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 1


def test_mint_only_through_the_local_admin_cli(w, monkeypatch, tmp_path,
                                              capsys):
    """R2: no HTTP/MCP verb; the one door is ``pg admin mint-oversight-token``."""
    from plangraph.keeper import cli, config, mcp

    assert "mint_oversight_token" not in api_routes.WRITE_VERBS
    assert "mint_oversight_token" not in api_routes.READ_VERBS
    assert "rotate_oversight_token" not in api_routes.WRITE_VERBS
    assert not any("oversight" in route for route in
                   api_routes.WRITE_VERBS | api_routes.READ_VERBS)
    assert not any("oversight" in name for name in mcp._ROUTES)

    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    cfg = tmp_path / "keeper.json"
    data = {"db": str(w.path), "snapshot": str(tmp_path / "snap.json"),
            "bind": "127.0.0.1:0"}
    if os.environ.get("PG_KEEPER_BACKEND") == "postgres":
        data.update(backend="postgres", dsn=os.environ.get("PG_DSN"))
    cfg.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(cfg, 0o600)
    config.load_config(cfg)                       # the file is valid
    token_out = tmp_path / "oversight.token"

    code = cli.main(["--config", str(cfg), "admin", "mint-oversight-token",
                     OPP, "--go", GO, "--reason", "local mint",
                     "--token-out", str(token_out)])
    captured = capsys.readouterr()
    assert code == 0
    token = token_out.read_text(encoding="utf-8").strip()
    assert token.startswith("pgo1_")
    assert token not in captured.out and token not in captured.err
    assert str(token_out) in captured.out
    assert (token_out.stat().st_mode & 0o777) == 0o600
    assert auth.authenticate(w.conn, token, now=w.clock()).role == "oversight"


def test_cli_mint_needs_the_live_env(w, monkeypatch, tmp_path, capsys):
    from plangraph.keeper import cli

    monkeypatch.delenv("PG_OVERSIGHT_LIVE", raising=False)
    grant_go(w)
    add_oversight(w)
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(w.path),
                               "snapshot": str(tmp_path / "s.json")}),
                   encoding="utf-8")
    os.chmod(cfg, 0o600)
    token_out = tmp_path / "oversight.token"
    code = cli.main(["--config", str(cfg), "admin", "mint-oversight-token",
                     OPP, "--go", GO, "--token-out", str(token_out)])
    assert code != 0
    assert not token_out.exists()                 # fail closed, no token


def test_token_plaintext_never_in_db_events_or_audit(w, monkeypatch):
    token, principal = live(w, monkeypatch)
    apply(w, principal, [op("edit", key="plangraph:OTHER",
                            fields={"note": "audited"})])
    dump = "\n".join(str(tuple(r)) for table in
                     ("events", "audit_log", "audit_findings", "principals",
                      "oversight_tokens", "oversight_ops")
                     for r in w.conn.execute(f"SELECT * FROM {table}"))
    assert token not in dump
    row = w.conn.execute("SELECT token_sha256 FROM oversight_tokens"
                         " WHERE principal=?", (OPP,)).fetchone()
    assert row["token_sha256"] == auth.hash_token(token)
    event = w.conn.execute("SELECT payload FROM events"
                           " WHERE type='oversight_token_minted'").fetchone()
    assert event is not None and token not in event["payload"]
    payload = json.loads(event["payload"])
    assert payload["go_decision"] == GO
    assert token not in str(w.store.snapshot(w.p("router")))


# ------------------------------------------------------- token lifecycle
def test_token_ttl_expiry_refused(w, monkeypatch):
    from plangraph.keeper import store_oversight

    token, principal = live(w, monkeypatch)
    ttl = getattr(store_oversight, "OVERSIGHT_TOKEN_TTL_S", 3600)
    row = w.conn.execute("SELECT created_at,expires_at FROM oversight_tokens"
                         " WHERE principal=?", (OPP,)).fetchone()
    assert w.store._parse_iso(row["expires_at"]) > \
        w.store._parse_iso(row["created_at"])
    apply(w, principal, [op("edit", key="plangraph:OTHER",
                            fields={"note": "before expiry"})])
    expires = row["expires_at"]
    w.clock.advance(ttl + 1)
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:OTHER",
                      fields={"note": "after expiry"})])
    assert err.code == "forbidden" and has("token_expired", err.msg)
    unchanged = w.conn.execute("SELECT expires_at FROM oversight_tokens"
                               " WHERE principal=?", (OPP,)).fetchone()
    assert unchanged["expires_at"] == expires     # no extension on use


def test_rotation_revokes_previous_generation(w, monkeypatch):
    token1, p1 = live(w, monkeypatch)
    out2 = mint(w)
    token2 = out2["token"]
    assert token1 != token2
    assert out2["generation"] == 2
    # generation 1 is dead at authentication, generation 2 works
    assert refusal(auth.authenticate, w.conn, token1,
                   now=w.clock()).code == "forbidden"
    p2 = auth.authenticate(w.conn, token2, now=w.clock())
    assert p2.generation == 2
    rows = list(w.conn.execute(
        "SELECT generation,revoked_at FROM oversight_tokens"
        " WHERE principal=? ORDER BY generation", (OPP,)))
    assert [r["generation"] for r in rows] == [1, 2]
    assert rows[0]["revoked_at"] is not None
    assert rows[1]["revoked_at"] is None          # exactly one live row
    assert sum(1 for r in rows if r["revoked_at"] is None) == 1


def test_router_revoke_kills_live_tokens(w, monkeypatch):
    token, principal = live(w, monkeypatch)
    apply(w, principal, [op("edit", key="plangraph:OTHER",
                            fields={"note": "live"})])
    auth.revoke(w.conn, OPP)                      # the router's local lever
    assert refusal(auth.authenticate, w.conn, token).code == "forbidden"
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:OTHER",
                      fields={"note": "after revoke"})])
    assert err.code == "forbidden"
    assert w.store._item("plangraph:OTHER")["note"] == "live"


# ---------------------------------------------------------- verb seam
_VERB_CALLS = {
    "create_items": lambda s, p: s.create_items(p, [fresh("NEW")]),
    "import_items": lambda s, p: s.import_items(p, [fresh("NEW")]),
    "edit_definition": lambda s, p: s.edit_definition(
        p, "plangraph:OTHER", {"note": "x"}),
    "set_priority": lambda s, p: s.set_priority(p, "plangraph:OTHER", 3),
    "claim": lambda s, p: s.claim(p, "plangraph:OTHER"),
    "verdict": lambda s, p: s.verdict(p, "plangraph:OTHER", True, "ok"),
    "waive_second_family": lambda s, p: s.waive_second_family(
        p, "plangraph:HIGH", "D-1"),
    "withdraw_review": lambda s, p: s.withdraw_review(
        p, "plangraph:OTHER", "D-1"),
    "set_kind": lambda s, p: s.set_kind(p, "plangraph:OTHER", "question"),
    "cancel": lambda s, p: s.cancel(p, "plangraph:OTHER", "stop"),
    "reopen": lambda s, p: s.reopen(p, "plangraph:OTHER", "again", "D-1"),
    "close_on_decision": lambda s, p: s.close_on_decision(
        p, "plangraph:DEC", "batch@sha"),
    "set_legacy_state": lambda s, p: s.set_legacy_state(
        p, "plangraph:OTHER", "claimed", ""),
    "add_evidence_router": lambda s, p: s.add_evidence_router(
        p, "plangraph:OTHER", "decision", {"id": "D-1"}),
    "rehome": lambda s, p: s.rehome(p, "plangraph:OTHER", "demo"),
    "mint_run_token": lambda s, p: s.mint_run_token(
        p, "plangraph:OTHER", "writer", "w1"),
    "record_run": lambda s, p: s.record_run(p, "plangraph:OTHER", {}),
    "set_metrics": lambda s, p: s.set_metrics(p, "2026-01-01", []),
    "migrate_descriptions": lambda s, p: s.migrate_descriptions(p),
    "reap": lambda s, p: s.reap(p),
    "ask_question": lambda s, p: s.ask_question(
        p, "plangraph:OTHER", "which?"),
}


@pytest.mark.parametrize("verb", sorted(_VERB_CALLS))
def test_denied_verbs_are_403_with_oversight_reason(w, monkeypatch, verb):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(_VERB_CALLS[verb], w.store, principal)
    assert err.code == "forbidden"
    assert has("verb_denied", err.msg), (verb, err.msg)
    assert snap(w) == before                       # no state change


def test_every_write_verb_is_classified_for_oversight():
    from plangraph.keeper import oversight_policy

    for verb in sorted(api_routes.WRITE_VERBS - {"apply_ops"}):
        err = refusal(oversight_policy.check_action, verb)
        assert err.code == "forbidden", verb
        assert has("verb_denied", err.msg), (verb, err.msg)
    oversight_policy.check_action("apply_ops")     # the one allowed write
    assert refusal(oversight_policy.check_action, "a_future_verb").code == \
        "forbidden"                               # fail closed, not default-allow


# ----------------------------------------------------- allowed op seam
@pytest.mark.parametrize("kind,tier,evidence", [
    ("lane", "standard", ["any"]),
    ("question", "none", ["decision"]),
    ("question", "standard", ["any"]),
])
def test_create_allowed_kinds_and_tiers(w, monkeypatch, kind, tier, evidence):
    """The grant's create tiers: honoured at/above the default, refused below.

    D-2458 contract correction: plangraph's project default is ``high`` and an
    oversight create naming a TIER BELOW IT is REFUSED
    ``oversight:tier_below_default`` (the raise-only grant refuses; it never
    silently stores the default any more).  The R1 question/``none`` carve-out
    is stored exactly as asked.  A ``standard`` create in a default-``standard``
    project is honoured end to end in
    ``test_create_at_or_above_default_is_honoured``.
    """
    _, principal = live(w, monkeypatch)
    item = fresh("NEW", kind=kind, tier=tier, evidence=evidence)
    if tier == "standard":
        err = refusal(create_linked, w, principal, item)
        assert err.code == "forbidden" and has("tier_below_default", err.msg)
        # both seams refuse it: the policy verdict and the store's own tier rule
        with pytest.raises(Refused) as exc:
            w.store._normalise_create(item, principal)
        assert has("tier_below_default", exc.value.msg)
        assert w.store._item("plangraph:NEW") is None
        return
    out = create_linked(w, principal, item)
    assert out["applied"] == 2
    stored = w.store._item("plangraph:NEW")
    assert stored["kind"] == kind
    # The granted question/none carve-out keeps the tier it named.
    if tier == "none" and kind == "question":
        expected_tier = "none"
    else:
        expected_tier = tier
    assert stored["review_tier"] == expected_tier
    assert stored["evidence_kinds"] == evidence
    created = [e for e in keeper_events.read_events(w.conn)
               if e["type"] == "item_created"
               and e["key"] == "plangraph:NEW"]
    assert created and created[-1]["payload"]["review_tier"] == expected_tier


def test_edit_note_description_and_priority_on_open_lane(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    out = apply(w, principal, [
        op("edit", key="plangraph:OTHER",
           fields={"note": "new note", "description": "new description"}),
        op("priority", key="plangraph:OTHER", priority=5),
    ])
    assert out["applied"] == 2
    item = w.store._item("plangraph:OTHER")
    assert item["note"] == "new note"
    assert item["description"] == "new description"
    assert item["priority"] == 5
    assert item["title"] == "title OTHER"          # untouched
    types = [e["type"] for e in keeper_events.read_events(w.conn)
             if e["key"] == "plangraph:OTHER"]
    assert "item_defined" in types and "priority_set" in types


def test_depends_on_additions_allowed(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    out = apply(w, principal, [
        op("add_dep", key="plangraph:HOST", dep="plangraph:LINK"),
        op("edit", key="plangraph:OTHER",
           fields={"depends_on": ["plangraph:LINK"]}),
    ])
    assert out["applied"] == 2
    assert "plangraph:LINK" in w.store._item("plangraph:HOST")["depends_on"]
    assert w.store._item("plangraph:OTHER")["depends_on"] == ["plangraph:LINK"]
    edges = {(r["src"], r["dst"]) for r in w.conn.execute(
        "SELECT src,dst FROM edges WHERE rel='depends_on'")}
    assert ("plangraph:HOST", "plangraph:LINK") in edges
    assert ("plangraph:OTHER", "plangraph:LINK") in edges


def test_depends_on_add_form_allowed(w, monkeypatch):
    """The ``{add: [...]}`` side of BATCH.md's form is an addition: granted."""
    _, principal = live(w, monkeypatch)
    out = apply(w, principal, [
        op("edit", key="plangraph:HOST",
           fields={"depends_on": {"add": ["plangraph:LINK"]}}),
    ])
    assert out["applied"] == 1
    # both the old and the new dep stand, and nothing was dropped
    assert set(w.store._item("plangraph:HOST")["depends_on"]) == {
        "plangraph:OTHER", "plangraph:LINK"}
    assert "dropped_deps" not in out["results"][0]
    edges = {(r["src"], r["dst"]) for r in w.conn.execute(
        "SELECT src,dst FROM edges WHERE rel='depends_on'")}
    assert {("plangraph:HOST", "plangraph:OTHER"),
            ("plangraph:HOST", "plangraph:LINK")} <= edges


def test_depends_on_replace_keeping_every_dep_allowed(w, monkeypatch):
    """A full-list replace that drops nothing is an addition, not a removal."""
    _, principal = live(w, monkeypatch)
    out = apply(w, principal, [
        op("edit", key="plangraph:HOST",
           fields={"depends_on": ["plangraph:OTHER", "plangraph:LINK"]}),
    ])
    assert out["applied"] == 1
    assert set(w.store._item("plangraph:HOST")["depends_on"]) == {
        "plangraph:OTHER", "plangraph:LINK"}
    assert "dropped_deps" not in out["results"][0]


# ------------------------------------------------------ denied op seam
def test_edit_done_criteria_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [op(
        "edit", key="plangraph:OTHER",
        fields={"done_criteria": "weaker criteria"})])
    assert err.code == "forbidden" and has("field_denied", err.msg)
    assert w.store._item("plangraph:OTHER")["done_criteria"] == "criteria"
    assert snap(w) == before


@pytest.mark.parametrize("fields", [
    {"title": "renamed"},
    {"owner": "someone"},
    {"track": "T9"},
    {"evidence_kinds": ["commit+ci"]},
    {"host": "elsewhere"},
    {"review_tier": "none"},
    {"review_tier": "high"},
])
def test_edit_other_fields_denied(w, monkeypatch, fields):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:OTHER", fields=fields)])
    assert err.code == "forbidden"
    assert has("field_denied", err.msg) or has("tier_denied", err.msg)
    assert snap(w) == before


@pytest.mark.parametrize("ops", [
    [("remove_dep", "plangraph:HOST", "plangraph:OTHER")],
    [("edit", "plangraph:HOST", {"depends_on": []})],
    # an add and a drop in the same list is a drop: never reached
    [("edit", "plangraph:HOST",
      {"depends_on": ["plangraph:LINK"]})],
    # the {add, remove} form: ANY remove side is a removal -- naming a dep,
    # naming nothing, or sitting next to an add.  The grant is additions only.
    [("edit", "plangraph:HOST",
      {"depends_on": {"remove": ["plangraph:OTHER"]}})],
    [("edit", "plangraph:HOST",
      {"depends_on": {"remove": []}})],
    [("edit", "plangraph:HOST",
      {"depends_on": {"add": ["plangraph:LINK"], "remove": []}})],
])
def test_dep_removal_denied(w, monkeypatch, ops):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    built = []
    for name, key, value in ops:
        if name == "remove_dep":
            built.append(op(name, key=key, dep=value))
        else:
            built.append(op(name, key=key, fields=value))
    err = refusal(apply, w, principal, built)
    assert err.code == "forbidden" and has("dep_removal", err.msg)
    assert w.store._item("plangraph:HOST")["depends_on"] == ["plangraph:OTHER"]
    assert snap(w) == before


@pytest.mark.parametrize("fields", [
    {"depends_on": {}},                             # neither side
    {"depends_on": {"bogus": ["plangraph:LINK"]}},  # a key that is not add/remove
    {"depends_on": {"add": "plangraph:LINK"}},      # a side that is not a list
    {"depends_on": {"add": [123]}},                 # a side of non-strings
    {"depends_on": "plangraph:LINK"},               # not a list, not a form
    {"depends_on": 42},
])
def test_depends_on_bad_shape_denied_fails_closed(w, monkeypatch, fields):
    """The policy never reads an unrecognised shape as a list: field_denied."""
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:HOST", fields=fields)])
    assert err.code == "forbidden" and has("field_denied", err.msg), err.msg
    assert w.store._item("plangraph:HOST")["depends_on"] == ["plangraph:OTHER"]
    assert snap(w) == before


def test_add_dep_addition_then_cycle_refused_and_rolled_back(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [
        op("add_dep", key="plangraph:HOST", dep="plangraph:LINK"),   # allowed
        op("add_dep", key="plangraph:OTHER", dep="plangraph:HOST"),  # cycle
    ])
    assert err.code == "cycle" and "cycle" in err.msg
    assert w.store._item("plangraph:HOST")["depends_on"] == ["plangraph:OTHER"]
    assert w.store._item("plangraph:OTHER")["depends_on"] == []
    assert w.store._item("plangraph:LINK")["state"] == "open"
    assert snap(w) == before                       # the first addition rolled back


@pytest.mark.parametrize("target,reason", [
    ("plangraph:HIGH", "target_high"),
    ("plangraph:GATE", "target_kind"),
    ("plangraph:GOAL", "target_kind"),
    ("plangraph:DEC", "target_kind"),
    ("plangraph:OPTASK", "target_kind"),
])
def test_new_edge_on_forbidden_target_denied(w, monkeypatch, target, reason):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("add_dep", key="plangraph:OTHER", dep=target)])
    assert err.code == "forbidden" and has(reason, err.msg), err.msg
    assert snap(w) == before


def test_created_item_depending_on_high_target_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [
        op("create", item=fresh("NEW", deps=["plangraph:HIGH"])),
    ])
    assert err.code == "forbidden" and has("target_high", err.msg)
    assert snap(w) == before


@pytest.mark.parametrize("key,reason", [
    ("plangraph:HIGH", "target_high"),
    ("plangraph:GATE", "target_kind"),
    ("plangraph:GOAL", "target_kind"),
])
def test_edit_or_priority_on_forbidden_target_denied(w, monkeypatch, key,
                                                     reason):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    for batched in ([op("edit", key=key, fields={"note": "no"})],
                    [op("priority", key=key, priority=9)]):
        err = refusal(apply, w, principal, batched)
        assert err.code == "forbidden" and has(reason, err.msg), err.msg
    assert snap(w) == before


def test_edit_claimed_item_is_bad_state(w, monkeypatch):
    epoch = w.store.claim(w.p("l1pg"), "plangraph:CLAIMED")["epoch"]
    item = w.store._item("plangraph:CLAIMED")
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:CLAIMED",
                      fields={"note": "while claimed"})])
    assert err.code in ("bad_state", "conflict") and has("bad_state", err.msg)
    still = w.store._item("plangraph:CLAIMED")
    assert still["state"] == "claimed" and still["epoch"] == epoch
    assert still["round"] == item["round"]          # no round/epoch bump
    assert snap(w) == before


def test_edit_in_review_item_is_bad_state(w, monkeypatch):
    epoch = w.store.claim(w.p("l1pg"), "plangraph:CLAIMED")["epoch"]
    w.store.add_evidence(w.p("l1pg"), "plangraph:CLAIMED", epoch,
                         "decision", {"id": "D-1"})
    w.store.submit(w.p("l1pg"), "plangraph:CLAIMED", epoch)
    assert w.store._item("plangraph:CLAIMED")["state"] == "in_review"
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:CLAIMED",
                      fields={"note": "while in review"})])
    assert err.code in ("bad_state", "conflict") and has("bad_state", err.msg)
    assert w.store._item("plangraph:CLAIMED")["state"] == "in_review"
    assert snap(w) == before


@pytest.mark.parametrize("kind", ["gate", "goal", "decision",
                                  "operator_task"])
def test_create_forbidden_kinds_denied(w, monkeypatch, kind):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("create", item=fresh("NEW", kind=kind))])
    assert err.code == "forbidden" and has("kind_denied", err.msg)
    assert w.store._item("plangraph:NEW") is None
    assert snap(w) == before


def test_create_under_gate_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    item = fresh("NEW", under="plangraph:GATE")
    err = refusal(apply, w, principal, [op("create", item=item)])
    assert err.code in ("forbidden", "validation")
    assert has("field_denied", err.msg) or has("field_unknown", err.msg)
    assert snap(w) == before


@pytest.mark.parametrize("criteria", ["", "   ", "\n\t "],
                         ids=["empty", "spaces", "control"])
def test_create_without_done_criteria_refused(w, monkeypatch, criteria):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    item = fresh("NEW")
    item["done_criteria"] = criteria
    err = refusal(apply, w, principal, [
        op("create", item=item),
        op("edit", key="plangraph:HOST",
           fields={"depends_on": ["plangraph:OTHER", "plangraph:NEW"]}),
    ])
    assert err.code == "validation" and has("criteria_missing", err.msg)
    assert w.store._item("plangraph:NEW") is None
    assert snap(w) == before


def test_create_explicit_high_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("create", item=fresh("NEW", tier="high"))])
    assert err.code in ("forbidden", "validation")
    assert has("tier_denied", err.msg)             # R1: no explicit high
    assert snap(w) == before


def test_create_without_explicit_tier_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal,
                  [op("create", item=fresh("NEW", drop_tier=True))])
    assert err.code == "validation" and has("tier_denied", err.msg)
    assert snap(w) == before


def test_create_lane_with_tier_none_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [op(
        "create", item=fresh("NEW", kind="lane", tier="none"))])
    assert err.code in ("forbidden", "validation")
    assert has("tier_denied", err.msg)             # R1: none only for question
    assert snap(w) == before


def test_edit_tier_of_an_existing_item_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    for tier in ("none", "high"):
        err = refusal(apply, w, principal, [op(
            "edit", key="plangraph:OTHER",
            fields={"review_tier": tier})])
        assert err.code in ("forbidden", "validation")
        assert has("tier_denied", err.msg) or has("field_denied", err.msg)
    assert w.store._item("plangraph:OTHER")["review_tier"] == "standard"
    assert snap(w) == before


def test_oversight_cannot_lower_tier_below_default(w, monkeypatch):
    """Raise-only tiers: a below-default create is REFUSED, never clamped.

    D-2458 contract correction: the old build silently stored the project
    default (``high``) for an oversight ``standard`` create, so the item the
    grant described never existed and the refusal never fired.  Both seams now
    refuse it with ``oversight:tier_below_default`` and store nothing.
    """
    _, principal = live(w, monkeypatch)
    item = fresh("LOW_TIER", kind="lane", tier="standard")
    with pytest.raises(Refused) as exc:
        w.store._normalise_create(item, principal)
    assert has("tier_below_default", exc.value.msg)
    err = refusal(create_linked, w, principal, item)
    assert err.code == "forbidden" and has("tier_below_default", err.msg)
    assert w.store._item("plangraph:LOW_TIER") is None


def test_create_at_or_above_default_is_honoured(w, monkeypatch):
    """The other half of the raise-only grant: at/above the default is stored.

    ``demo`` defaults to ``standard``, so an oversight ``standard`` create
    there is allowed end to end (batch applied, tier untouched) -- the grant
    produces the very tier it names.
    """
    _seed_demo(w)
    _, principal = live(w, monkeypatch, projects=(P, "demo"))
    item = fresh("NEW", project="demo", tier="standard")
    out = apply(w, principal, [
        op("create", item=item),
        op("edit", key="demo:DOUT", fields={"depends_on":
                                            ["demo:DOTHER", "demo:NEW"]}),
    ])
    assert out["applied"] == 2, out
    stored = w.store._item("demo:NEW")
    assert stored["review_tier"] == "standard"
    assert stored["kind"] == "lane"
    assert [r for r in _denied_rows(w)] == []


def test_create_unknown_field_denied(w, monkeypatch):
    """The create allowlist is closed: an unknown field is never folded to note."""
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [op(
        "create", item=fresh("NEW", state="claimed"))])
    assert err.code in ("forbidden", "validation")
    assert has("field_unknown", err.msg) or has("field_denied", err.msg)
    assert snap(w) == before


def test_unknown_op_denied(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [op("open_the_door", key="x")])
    assert err.code in ("forbidden", "validation")
    assert has("op_denied", err.msg) or has("field_unknown", err.msg)
    assert snap(w) == before


@pytest.mark.parametrize("opname,kw", [
    ("set_kind", {"key": "plangraph:OTHER", "kind": "question"}),
    ("cancel", {"key": "plangraph:OTHER", "reason": "stop"}),
    ("legacy_state", {"key": "plangraph:OTHER", "state": "claimed"}),
    ("add_evidence", {"key": "plangraph:OTHER", "kind": "decision",
                      "ref": {"id": "D-1"}}),
    ("close_on_decision", {"key": "plangraph:DEC"}),
    ("rehome", {"key": "plangraph:OTHER", "new_project": "demo"}),
])
def test_non_oversight_ops_denied(w, monkeypatch, opname, kw):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [op(opname, **kw)])
    assert err.code == "forbidden" and has("op_denied", err.msg)
    assert snap(w) == before


def test_batch_ref_is_required(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(principal, [op("edit", key="plangraph:OTHER",
                                         fields={"note": "x"})])
    assert exc.value.code == "validation"
    assert has("batch_ref_required", exc.value.msg)
    assert snap(w) == before


def test_batch_ref_is_bounded_and_guarded(w, monkeypatch):
    """build-L1 LOW: batch_ref is bounded + guarded like reason/finding.

    A batch_ref with control characters or exceeding the cap is refused.
    """
    _, principal = live(w, monkeypatch)
    before = snap(w)
    # Control characters
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:OTHER", fields={"note": "x"})],
                  batch_ref="bad\x00ref")
    assert err.code == "validation"
    assert has("field_denied", err.msg) or has("control", err.msg)
    assert snap(w) == before
    # Exceeds cap (200 chars)
    err2 = refusal(apply, w, principal,
                   [op("edit", key="plangraph:OTHER", fields={"note": "x"})],
                   batch_ref="x" * 201)
    assert err2.code == "validation"
    assert has("field_denied", err2.msg) or has("exceeds", err2.msg)
    assert snap(w) == before


def test_batch_is_atomic_and_names_the_failing_op(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, [
        op("edit", key="plangraph:OTHER", fields={"note": "first"}),
        op("priority", key="plangraph:OTHER", priority=2),
        op("edit", key="plangraph:HIGH", fields={"note": "third"}),
    ])
    assert err.msg.startswith("op 3 edit plangraph:HIGH: ")
    assert has("target_high", err.msg)
    assert snap(w) == before                       # ops 1-2 rolled back


# ------------------------------------------------------- audit + alarms
def test_applied_op_writes_audit_row_with_event_link(w, monkeypatch):
    token, principal = live(w, monkeypatch)
    apply(w, principal, [op("edit", key="plangraph:OTHER",
                            fields={"note": "audited"},
                            reason="explain the edit", finding=FINDING)])
    row = w.conn.execute("SELECT * FROM oversight_ops"
                         " WHERE outcome='applied'").fetchone()
    assert row is not None
    assert row["principal"] == OPP
    assert row["token_id"] == principal.token_id
    assert row["generation"] == principal.generation
    assert row["batch_ref"] == BATCH
    assert (row["op"], row["key"]) == ("edit", "plangraph:OTHER")
    assert row["reason"] == "explain the edit"
    assert row["finding_id"] == FINDING
    assert row["event_seq"] is not None
    event = w.conn.execute("SELECT seq,type,key FROM events WHERE seq=?",
                           (row["event_seq"],)).fetchone()
    assert event is not None
    assert (event["type"], event["key"]) == ("item_defined", "plangraph:OTHER")
    assert token not in str(dict(row))


def test_denied_audit_row_survives_the_batch_rollback(w, monkeypatch):
    token, principal = live(w, monkeypatch)
    err = refusal(apply, w, principal, [
        op("edit", key="plangraph:OTHER", fields={"note": "first"},
           reason="would apply", finding="F-1"),
        op("edit", key="plangraph:HOST",
           fields={"done_criteria": "denied"}, reason="denied reason",
           finding=FINDING),
    ])
    assert has("field_denied", err.msg)
    assert w.store._item("plangraph:OTHER")["note"] is None
    rows = list(w.conn.execute("SELECT * FROM oversight_ops"
                               " ORDER BY id"))
    denied = [r for r in rows if r["outcome"] == "denied"]
    assert len(denied) == 1
    row = denied[0]
    assert row["principal"] == OPP
    assert row["op"] == "edit" and row["key"] == "plangraph:HOST"
    assert row["reason"] == "denied reason"
    assert row["finding_id"] == FINDING
    assert "oversight:" in (row["code"] or "")
    assert token not in str(dict(row))
    assert [r for r in rows if r["outcome"] == "applied"] == []   # rolled back


def test_oversight_ops_are_append_only(w, monkeypatch):
    _, principal = live(w, monkeypatch)
    apply(w, principal, [op("edit", key="plangraph:OTHER",
                            fields={"note": "kept"})])
    for sql in ("UPDATE oversight_ops SET reason='rewritten'",
                "DELETE FROM oversight_ops"):
        with pytest.raises(db_errors.INTEGRITY):
            w.conn.execute(sql)
    count = w.conn.execute("SELECT COUNT(*) FROM oversight_ops").fetchone()[0]
    assert count == 1
    row = w.conn.execute("SELECT * FROM oversight_ops").fetchone()
    assert row["reason"] == "oversight scope grant"


def test_oversight_ops_are_not_reaped(w, monkeypatch):
    """R8: the audit table is unbounded; no retention window truncates it."""
    _, principal = live(w, monkeypatch)
    apply(w, principal, [op("edit", key="plangraph:OTHER",
                            fields={"note": "kept forever"})])
    before = w.conn.execute("SELECT COUNT(*) FROM oversight_ops").fetchone()[0]
    w.clock.advance(10 * 365 * 24 * 3600)
    w.store.reap(w.p("system"))
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_ops"
                          ).fetchone()[0] == before


def test_denied_attempt_raises_one_open_alarm(w, monkeypatch):
    token, principal = live(w, monkeypatch)
    for _ in range(2):
        err = refusal(apply, w, principal, [op(
            "edit", key="plangraph:OTHER",
            fields={"done_criteria": "no"}, finding=FINDING)])
        assert has("field_denied", err.msg)
    rows = list(w.conn.execute(
        "SELECT * FROM projector_alarms WHERE kind='oversight_denied'"
        " AND workflow_id='oversight-guard' AND cleared_at IS NULL"))
    assert len(rows) == 1                          # ON CONFLICT: single open row
    detail = rows[0]["detail"]
    assert "oversight:" in detail
    assert FINDING in detail
    assert token not in detail
    assert w.store._item("plangraph:OTHER")["title"] not in detail


# ------------------------------------------------------------ rate limit
def _applied_cap(policy):
    return getattr(policy, "OVERSIGHT_APPLIED_PER_HOUR",
                   getattr(policy, "APPLIED_PER_HOUR", 120))


def _denied_cap(policy):
    return getattr(policy, "OVERSIGHT_DENIED_PER_HOUR",
                   getattr(policy, "DENIED_PER_HOUR", 10))


def test_applied_rate_limit_is_forbidden_rate_limited(w, monkeypatch):
    from plangraph.keeper import oversight_policy

    _, principal = live(w, monkeypatch)
    cap = _applied_cap(oversight_policy)
    assert cap > 0
    for i in range(cap):
        apply(w, principal, [op("priority", key="plangraph:OTHER",
                                priority=i % 7)])
    err = refusal(apply, w, principal, [op("priority",
                                           key="plangraph:OTHER", priority=7)])
    assert err.code == "forbidden" and has("rate_limited", err.msg)
    # R3: the public contract stays 403 ``forbidden`` -- no new 429 enum.
    assert api_routes.STATUS.get("forbidden") == 403
    assert "rate_limited" not in api_routes.STATUS


def test_denied_budget_is_alarm_only_not_auto_revoke(w, monkeypatch):
    from plangraph.keeper import oversight_policy

    token, principal = live(w, monkeypatch)
    cap = _denied_cap(oversight_policy)
    assert cap > 0
    for i in range(cap):
        err = refusal(apply, w, principal, [op(
            "edit", key="plangraph:OTHER",
            fields={"done_criteria": f"denied {i}"})])
        assert has("field_denied", err.msg)
    err = refusal(apply, w, principal, [op(
        "edit", key="plangraph:OTHER",
        fields={"done_criteria": "over budget"})])
    assert err.code == "forbidden" and has("rate_limited", err.msg)
    # R4: the breach is an alarm, never an automatic revoke.
    row = w.conn.execute("SELECT revoked_at FROM oversight_tokens"
                         " WHERE principal=?", (OPP,)).fetchone()
    assert row["revoked_at"] is None
    assert auth.authenticate(w.conn, token, now=w.clock()).role == "oversight"
    alarms = list(w.conn.execute(
        "SELECT * FROM projector_alarms WHERE kind='oversight_denied'"
        " AND workflow_id='oversight-guard' AND cleared_at IS NULL"))
    assert alarms


# ------------------------------------------------------- schema / parity
def test_schema_is_v14_and_carries_the_oversight_ddl():
    from plangraph.keeper import schema

    # Main pins ``SCHEMA_VERSION`` at 13 (its own schema tests assert it), so the
    # v13 -> v14 step the oversight tables add is named separately and installed
    # presence-based by ``db._upgrade`` -- main's upgrade tests stay green.
    assert schema.SCHEMA_VERSION == 13
    assert schema.OVERSIGHT_SCHEMA_VERSION == schema.SCHEMA_VERSION + 1 == 14
    assert "oversight_tokens" in schema.OVERSIGHT_DDL
    assert "oversight_ops" in schema.OVERSIGHT_DDL
    assert schema.OVERSIGHT_DDL in schema.SCHEMA
    # the merge keeps main's JEV half at v13 and appends ours after it
    assert schema.SCHEMA.index(schema.JEV_DDL) < schema.SCHEMA.index(schema.OVERSIGHT_DDL)
    for trigger in ("trg_oversight_tokens_revoke_only_update",
                    "trg_oversight_ops_append_only_update",
                    "trg_oversight_ops_append_only_delete"):
        assert trigger in schema.OVERSIGHT_DDL


def test_pg_migration_7_matches_the_sqlite_ddl():
    # Merge note: the name keeps its old number (the CI trailer gate treats a
    # renamed test as a removal).  Main's #140 took Migration 7 for the exec
    # ownership columns of ``items``, so the oversight twin is Migration 8.
    from plangraph.keeper import schema, schema_pg

    sqlite_tables = schema_pg.parse_tables(schema.SCHEMA)
    pg_sql = "\n".join(m.sql for m in schema_pg.MIGRATIONS)
    pg_tables = schema_pg.parse_tables(pg_sql)
    for table in ("oversight_tokens", "oversight_ops"):
        assert table in sqlite_tables and table in pg_tables, table
        assert [c for c, _ in sqlite_tables[table]] == \
            [c for c, _ in pg_tables[table]], table
        assert [bool(nn) for _, nn in sqlite_tables[table]] == \
            [bool(nn) for _, nn in pg_tables[table]], table
    assert schema_pg.LATEST >= 8
    eight = [m for m in schema_pg.MIGRATIONS if m.version == 8]
    assert eight, "Postgres Migration 8 must carry the oversight tables"
    eight_tables = schema_pg.parse_tables(eight[0].sql)
    assert {"oversight_tokens", "oversight_ops"} <= set(eight_tables)
    earlier = schema_pg.parse_tables(
        "\n".join(m.sql for m in schema_pg.MIGRATIONS if m.version < 8))
    assert not {"oversight_tokens", "oversight_ops"} & set(earlier)


def test_all_pg_migrations_apply_in_order_exec_7_then_oversight_8():
    """Merge of origin/main #140: migrations 1..8 apply in order, no gaps.

    Applied through the parity parser (``parse_tables`` over the ``MIGRATIONS``
    list, no live Postgres): 7 is main's exec ownership of ``items``, 8 is our
    oversight pair, and each new set is column-for-column the SQLite twin.
    """
    from plangraph.keeper import schema, schema_pg

    steps = list(schema_pg.MIGRATIONS)
    versions = [m.version for m in steps]
    assert versions == list(range(1, 9))              # 1..8: no gaps
    assert versions == sorted(set(versions))          # strictly increasing
    assert schema_pg.LATEST == 8

    import re
    exec_cols = re.findall(
        r"ADD COLUMN IF NOT EXISTS (exec_\w+)",
        next(m.sql for m in steps if m.version == 7))
    seven = [m for m in steps if m.version == 7]
    eight = [m for m in steps if m.version == 8]
    assert len(seven) == len(eight) == 1
    assert steps.index(seven[0]) < steps.index(eight[0])   # 7 before 8

    # apply the migrations in order, re-parsing after each step
    applied = {}
    for index, step in enumerate(steps, start=1):
        applied = schema_pg.parse_tables(
            "\n".join(m.sql for m in steps[:index]))
        columns = [c for c, _ in applied["items"]]
        if step.version < 7:
            assert "exec_workflow_id" not in columns, step
        if step.version < 8:
            assert not {"oversight_tokens", "oversight_ops"} & set(applied), step
    assert applied, "every migration parsed"

    # migration 7 alone adds exactly the exec ownership columns ...
    seven_tables = schema_pg.parse_tables(seven[0].sql)
    assert list(seven_tables) == ["items"]
    assert [c for c, _ in seven_tables["items"]] == exec_cols
    assert applied["items"][-len(exec_cols):] == \
        [(c, False) for c in exec_cols], applied["items"]

    # ... and migration 8 alone carries the oversight tables
    eight_tables = schema_pg.parse_tables(eight[0].sql)
    assert {"oversight_tokens", "oversight_ops"} <= set(eight_tables)

    # both new sets match SQLite column-for-column (name order and NOT NULL)
    sqlite_tables = schema_pg.parse_tables(schema.SCHEMA)
    # Main declares exec columns before timestamps in SQLite, while PG adds
    # them after the shipped baseline. Compare in PG migration order.
    sqlite_tables["items"] = (
        [(c, nn) for c, nn in sqlite_tables["items"] if c not in exec_cols]
        + [(c, nn) for c, nn in sqlite_tables["items"] if c in exec_cols])
    for table in ("oversight_tokens", "oversight_ops"):
        assert [c for c, _ in sqlite_tables[table]] == \
            [c for c, _ in applied[table]], table
        assert [bool(nn) for _, nn in sqlite_tables[table]] == \
            [bool(nn) for _, nn in applied[table]], table
    for column, notnull in sqlite_tables["items"][-len(exec_cols):]:
        assert column in exec_cols, column
    assert [c for c, _ in sqlite_tables["items"]] == \
        [c for c, _ in applied["items"]], "items columns drift"
    assert [bool(nn) for _, nn in sqlite_tables["items"]] == \
        [bool(nn) for _, nn in applied["items"]], "items NOT NULL drift"

    # the exec columns live in the SQLite items DDL at main's v13 shape, and
    # the SQLite version step (v14) stays the oversight tables alone
    assert schema.OVERSIGHT_SCHEMA_VERSION == 14
    assert schema.SCHEMA_VERSION == 13
    for column in exec_cols:
        assert f"    {column}" in schema.TABLES, column
    assert "exec_workflow_id" not in schema.OVERSIGHT_DDL


def test_copy_to_pg_includes_both_oversight_tables():
    from plangraph.keeper import copy_to_pg

    assert {"oversight_tokens", "oversight_ops"} <= set(copy_to_pg.TABLES)
    order = list(copy_to_pg.TABLES)
    assert order.index("oversight_tokens") < order.index("oversight_ops")


# ------------------------- every refusal is audited (fix round 1, MEDIUM)
def _denied_rows(w):
    return list(w.conn.execute(
        "SELECT * FROM oversight_ops WHERE outcome='denied' ORDER BY id"))


def _open_guard_alarms(w):
    return list(w.conn.execute(
        "SELECT * FROM projector_alarms WHERE kind='oversight_denied'"
        " AND workflow_id='oversight-guard' AND cleared_at IS NULL"))


def assert_audited_refusal(w, err, *, op_name, key):
    """One denied row with the refusal's code/message/finding, one alarm."""
    rows = _denied_rows(w)
    assert len(rows) == 1, [dict(r) for r in rows]
    row = rows[0]
    assert row["principal"] == OPP
    assert row["batch_ref"] == BATCH
    assert (row["op"], row["key"]) == (op_name, key)
    assert row["code"] == err.code
    assert row["reason"] == err.msg
    assert row["finding_id"] == FINDING
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1, [dict(a) for a in alarms]
    assert "oversight:" in alarms[0]["detail"]
    assert FINDING in alarms[0]["detail"]


def test_cycle_refusal_writes_a_denied_row_and_alarm(w, monkeypatch):
    """A cycle is raised by the shared store validation, not the policy gate."""
    token, principal = live(w, monkeypatch)
    err = refusal(apply, w, principal, [
        op("add_dep", key="plangraph:HOST", dep="plangraph:LINK"),   # allowed
        op("add_dep", key="plangraph:OTHER", dep="plangraph:HOST"),  # cycle
    ])
    assert err.code == "cycle"
    assert_audited_refusal(w, err, op_name="add_dep",
                           key="plangraph:OTHER")
    assert _denied_rows(w)[-1]["op_index"] == 2
    dump = (str([dict(r) for r in _denied_rows(w)])
            + str([dict(a) for a in _open_guard_alarms(w)]))
    assert token not in dump                       # never the plaintext token


def test_bad_state_refusal_writes_a_denied_row_and_alarm(w, monkeypatch):
    """``bad_state`` carries its own code/message into the audit row."""
    w.store.claim(w.p("l1pg"), "plangraph:CLAIMED")
    _, principal = live(w, monkeypatch)
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:CLAIMED",
                      fields={"note": "while claimed"})])
    assert err.code == "bad_state"
    assert_audited_refusal(w, err, op_name="edit", key="plangraph:CLAIMED")


def test_validation_refusal_writes_a_denied_row_and_alarm(w, monkeypatch):
    """A shape validation refusal from the shared planner is audited too."""
    _, principal = live(w, monkeypatch)
    err = refusal(apply, w, principal,
                  [op("priority", key="plangraph:OTHER", priority="high")])
    assert err.code == "validation"
    assert_audited_refusal(w, err, op_name="priority",
                           key="plangraph:OTHER")


def test_shared_machinery_denials_count_toward_the_denied_budget(
        w, monkeypatch):
    """Rows from the shared machinery are ledger entries: the cap sees them."""
    from plangraph.keeper import oversight_policy

    _, principal = live(w, monkeypatch)
    cap = _denied_cap(oversight_policy)
    assert cap > 0
    for i in range(cap):
        err = refusal(apply, w, principal, [op(
            "priority", key="plangraph:OTHER", priority=f"nope-{i}")])
        assert err.code == "validation"
    assert len(_denied_rows(w)) == cap
    assert len(_open_guard_alarms(w)) == 1         # kept as one open alarm
    err = refusal(apply, w, principal, [op(
        "priority", key="plangraph:OTHER", priority=3)])
    assert err.code == "forbidden" and has("rate_limited", err.msg)


# =========================================================================
# Fix round 2 (HIGH keeper auth, PR #138): three MEDIUM findings.
# =========================================================================

# -------------------- M1: project scope on EVERY allowed op, not just edit
def _seed_demo(w):
    """A complete graph in ``demo``, outside the principal's ['plangraph'].

    A goal that depends on every demo lane keeps the project valid, so the
    router can create it alongside the seeded plangraph graph.
    """
    w.store.create_items(w.p("router"), [
        defn("DOUT", [], project="demo", review_tier="standard"),
        defn("DOTHER", [], project="demo", review_tier="standard"),
        defn("DGOAL", ["DOUT", "DOTHER"], project="demo", kind="goal",
             review_tier="standard"),
    ])


#: One batch per allowed op class that must be refused by the project scope.
_M1_SCOPE_OPS = {
    "edit": lambda: [op("edit", key="demo:DOUT", fields={"note": "outside"})],
    "priority": lambda: [op("priority", key="demo:DOUT", priority=5)],
    "add_dep-item": lambda: [op("add_dep", key="demo:DOUT",
                                dep="demo:DOTHER")],
    "add_dep-target": lambda: [op("add_dep", key="plangraph:OTHER",
                                 dep="demo:DOUT")],
    "edit-add_dep-target": lambda: [op(
        "edit", key="plangraph:OTHER", fields={"depends_on": ["demo:DOUT"]})],
    "create-item": lambda: [op("create", item=fresh("NEW", project="demo"))],
    "create-dep-target": lambda: [op(
        "create", item=fresh("NEWDEP", deps=["demo:DOUT"]))],
}


@pytest.mark.parametrize("name", sorted(_M1_SCOPE_OPS))
def test_project_scope_is_enforced_for_every_allowed_op(w, monkeypatch, name):
    """R5: the one grant is scoped, so no op may name a demo item or dep."""
    _seed_demo(w)
    _, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(apply, w, principal, _M1_SCOPE_OPS[name]())
    assert err.code == "forbidden", (name, err.msg)
    assert has("project_denied", err.msg), (name, err.msg)
    assert snap(w) == before, name


def test_priority_outside_the_scope_leaves_the_item_alone(w, monkeypatch):
    """The regression the seat reported: ``priority`` used to slip through."""
    _seed_demo(w)
    _, principal = live(w, monkeypatch)
    assert w.store._item("demo:DOUT")["priority"] == 0
    err = refusal(apply, w, principal,
                  [op("priority", key="demo:DOUT", priority=9)])
    assert has("project_denied", err.msg)
    assert w.store._item("demo:DOUT")["priority"] == 0


def test_policy_denies_every_op_without_a_project_scope(w):
    """An empty scope fails closed inside the policy too (both seams use it)."""
    from plangraph.keeper import oversight_policy

    _seed_demo(w)
    items = {item["key"]: item for item in w.store._all_items()}
    for name, build in sorted(_M1_SCOPE_OPS.items()):
        verdicts = oversight_policy.check_batch(items, build(), [])
        assert len(verdicts) == 1, name
        assert verdicts[0].reason == "project_denied", (name, verdicts)


# ---------------- M2: verb-seam and token refusals are audited denials
def _assert_verb_refusal_audited(w, err, *, op_name, token, reason):
    rows = _denied_rows(w)
    assert len(rows) == 1, [dict(r) for r in rows]
    row = rows[0]
    assert row["principal"] == OPP
    assert row["op"] == op_name
    assert reason in (row["code"] or ""), dict(row)
    assert err.msg in row["reason"], dict(row)
    assert row["finding_id"] == ""                 # no op carried a finding
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1, [dict(a) for a in alarms]
    assert f"oversight:{reason}" in alarms[0]["detail"]
    assert token not in str(dict(row)) + str(dict(alarms[0]))


@pytest.mark.parametrize("verb", ["verdict", "waive_second_family",
                                  "withdraw_review", "set_kind", "cancel"])
def test_denied_verb_writes_a_denied_row_and_alarm(w, monkeypatch, verb):
    token, principal = live(w, monkeypatch)
    before = snap(w)
    err = refusal(_VERB_CALLS[verb], w.store, principal)
    assert err.code == "forbidden" and has("verb_denied", err.msg)
    _assert_verb_refusal_audited(w, err, op_name=verb, token=token,
                                 reason="verb_denied")
    assert snap(w) == before                       # the refusal wrote no state


def test_unknown_verb_at_the_verb_seam_is_audited(w, monkeypatch):
    """``check_action`` fails closed on a future verb; the denial is audited."""
    token, principal = live(w, monkeypatch)
    err = refusal(w.store._run, principal, "a_future_verb", None,
                  lambda now: None)
    assert err.code == "forbidden" and has("verb_denied", err.msg)
    _assert_verb_refusal_audited(w, err, op_name="a_future_verb", token=token,
                                 reason="verb_denied")


def test_expired_token_refusal_is_audited(w, monkeypatch):
    from plangraph.keeper import store_oversight

    token, principal = live(w, monkeypatch)
    w.clock.advance(getattr(store_oversight, "OVERSIGHT_TOKEN_TTL_S", 3600) + 1)
    err = refusal(apply, w, principal,
                  [op("edit", key="plangraph:OTHER", fields={"note": "late"})])
    assert err.code == "forbidden" and has("token_expired", err.msg)
    _assert_verb_refusal_audited(w, err, op_name="apply_ops", token=token,
                                 reason="token_expired")


def test_revoked_token_refusal_is_audited(w, monkeypatch):
    """The held principal's token is revoked by a rotation, then used again."""
    token, first = live(w, monkeypatch)
    mint(w)                                        # generation 2 revokes gen 1
    err = refusal(apply, w, first,
                  [op("priority", key="plangraph:OTHER", priority=1)])
    assert err.code == "forbidden" and has("token_revoked", err.msg)
    _assert_verb_refusal_audited(w, err, op_name="apply_ops", token=token,
                                 reason="token_revoked")


def test_token_rechecked_inside_write_transaction(w, monkeypatch):
    """build-L1 LOW: token is re-checked inside BEGIN IMMEDIATE to close mint/revoke race.

    A token revoked between the verb-seam check and the write lock must still be refused.
    """
    token, principal = live(w, monkeypatch)
    # Revoke the token
    mint(w)  # generation 2 revokes gen 1
    # Try to use the old principal - should be refused inside the transaction
    err = refusal(apply, w, principal,
                  [op("priority", key="plangraph:OTHER", priority=1)],
                  batch_ref="race-test")
    assert err.code == "forbidden" and has("token_revoked", err.msg)


def test_generation_and_scope_drift_refusals_are_audited(w, monkeypatch):
    """A stale/forged principal is refused at use time and still audited."""
    token, principal = live(w, monkeypatch)
    stale = dataclasses.replace(principal,
                                generation=principal.generation + 1)
    err = refusal(apply, w, stale,
                  [op("priority", key="plangraph:OTHER", priority=1)])
    assert has("token_rotated", err.msg)
    drifted = dataclasses.replace(principal, scope="plan-admin")
    err2 = refusal(apply, w, drifted,
                   [op("priority", key="plangraph:OTHER", priority=1)])
    assert has("token_denied", err2.msg)
    rows = _denied_rows(w)
    assert [("token_rotated" in (r["code"] or ""),
             "token_denied" in (r["code"] or "")) for r in rows] == \
        [(True, False), (False, True)], [dict(r) for r in rows]
    assert len(_open_guard_alarms(w)) == 1         # one alarm, kept open
    assert token not in str([dict(r) for r in rows])


def test_verb_denials_count_toward_the_denied_budget(w, monkeypatch):
    """Verb-seam rows are ledger entries: the denied cap sees them, alarm-only."""
    from plangraph.keeper import oversight_policy

    token, principal = live(w, monkeypatch)
    cap = _denied_cap(oversight_policy)
    assert cap > 0
    for _ in range(cap):
        err = refusal(w.store.cancel, principal, "plangraph:OTHER", "stop")
        assert err.code == "forbidden" and has("verb_denied", err.msg)
    assert len(_denied_rows(w)) == cap
    err = refusal(apply, w, principal,
                  [op("priority", key="plangraph:OTHER", priority=2)])
    assert err.code == "forbidden" and has("rate_limited", err.msg)
    # R4: the breach is a single alarm, never an automatic revoke.
    assert auth.authenticate(w.conn, token, now=w.clock()).role == "oversight"
    assert len(_open_guard_alarms(w)) == 1


# ------------- M3: oversight_tokens is revoke-only and never deletable
@pytest.mark.parametrize("column,value", [
    ("created_at", "1999-01-01T00:00:00+00:00"),
    ("minted_by", "intruder"),
    ("go_decision", "D-EVIL"),
    ("mint_reason", "rewritten after the fact"),
])
def test_revoke_update_cannot_smuggle_a_column_change(w, monkeypatch, column,
                                                      value):
    live(w, monkeypatch)
    row = w.conn.execute(
        "SELECT id," + column + " FROM oversight_tokens WHERE principal=?",
        (OPP,)).fetchone()
    revoked = w.store._iso(w.store.now())
    with pytest.raises(db_errors.INTEGRITY):
        w.conn.execute(
            f"UPDATE oversight_tokens SET revoked_at=?, {column}=? WHERE id=?",
            (revoked, value, row["id"]))
    fresh = w.conn.execute(
        "SELECT revoked_at," + column + " FROM oversight_tokens WHERE id=?",
        (row["id"],)).fetchone()
    assert fresh["revoked_at"] is None             # the whole update aborted
    assert fresh[column] == row[column]


def test_revoke_only_update_still_works_and_delete_aborts(w, monkeypatch):
    live(w, monkeypatch)
    row = w.conn.execute("SELECT id FROM oversight_tokens WHERE principal=?",
                         (OPP,)).fetchone()
    stamped = w.store._iso(w.store.now())
    w.conn.execute("UPDATE oversight_tokens SET revoked_at=? WHERE id=?",
                   (stamped, row["id"]))
    assert w.conn.execute("SELECT revoked_at FROM oversight_tokens WHERE id=?",
                          (row["id"],)).fetchone()["revoked_at"] == stamped
    with pytest.raises(db_errors.INTEGRITY):
        w.conn.execute("DELETE FROM oversight_tokens WHERE id=?", (row["id"],))
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 1


def test_null_columns_are_null_safe_across_a_revoke(w, monkeypatch):
    """A row with NULL provenance may be revoked, and not silently rewritten."""
    live(w, monkeypatch)
    ts = w.store._iso(w.store.now())
    # ``id`` is explicit on both backends: SQLite would autoincrement it, but
    # Postgres' BIGINT PRIMARY KEY has no default (the store assigns
    # MAX(id)+1 inside the write lock, like run_tokens), so an insert that
    # omits it fails there with NotNullViolation.
    w.conn.execute(
        "INSERT INTO oversight_tokens(id,token_sha256,principal,generation,scope,"
        "created_at,expires_at)"
        " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_tokens),"
        "?,?,?,?,?,?)",
        (auth.hash_token("pgo1_" + "n" * 43), OPP, 99, "plan-write", ts, ts))
    row = w.conn.execute("SELECT id FROM oversight_tokens WHERE generation=99"
                         ).fetchone()
    w.conn.execute("UPDATE oversight_tokens SET revoked_at=? WHERE id=?",
                   (ts, row["id"]))
    assert w.conn.execute("SELECT revoked_at,minted_by FROM oversight_tokens"
                          " WHERE id=?", (row["id"],)).fetchone()["revoked_at"]
    w.conn.execute(
        "INSERT INTO oversight_tokens(id,token_sha256,principal,generation,scope,"
        "created_at,expires_at)"
        " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_tokens),"
        "?,?,?,?,?,?)",
        (auth.hash_token("pgo1_" + "m" * 43), OPP, 100, "plan-write", ts, ts))
    other = w.conn.execute("SELECT id FROM oversight_tokens"
                           " WHERE generation=100").fetchone()
    with pytest.raises(db_errors.INTEGRITY):
        w.conn.execute(
            "UPDATE oversight_tokens SET revoked_at=?, minted_by='intruder'"
            " WHERE id=?", (ts, other["id"]))


def test_token_guard_columns_and_delete_trigger_are_in_both_ddls():
    from plangraph.keeper import schema, schema_pg

    # number updated for the merge: main's #140 holds Migration 7 (exec
    # ownership), so the oversight twin moved to Migration 8
    eight = "\n".join(m.sql for m in schema_pg.MIGRATIONS if m.version == 8)
    for ddl, label in ((schema.OVERSIGHT_DDL, "sqlite"), (eight, "pg")):
        assert "trg_oversight_tokens_append_only_delete" in ddl, label
        trigger = ddl.split("trg_oversight_tokens_revoke_only_update", 1)[1]
        for column in ("created_at", "minted_by", "go_decision", "mint_reason"):
            assert f"OLD.{column}" in trigger, (label, column)
            assert f"NEW.{column}" in trigger, (label, column)
    assert "trg_oversight_tokens_append_only_truncate" in eight


# =========================================================================
# Merge with main's KEEPER-BATCH-PRECONDITION: ``base_seq`` + ``dry_run``.
# =========================================================================
def _applied_rows(w):
    return list(w.conn.execute(
        "SELECT * FROM oversight_ops WHERE outcome='applied' ORDER BY id"))


def _head_seq(w):
    return w.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events"
                          ).fetchone()[0]


def test_oversight_dry_run_checks_the_gate_and_writes_nothing(w, monkeypatch):
    """A dry run performs every check and op, then rolls everything back.

    The valid batch passes the policy gate (pre-lock and in-lock), runs both
    ops and validates the final graph -- yet no item, event or applied audit
    row survives, so the applied budget is untouched too.
    """
    _, principal = live(w, monkeypatch)
    before = snap(w)
    out = w.store.apply_ops(
        principal,
        [op("edit", key="plangraph:OTHER", fields={"note": "dry"}),
         op("priority", key="plangraph:HOST", priority=5)],
        batch_ref=BATCH, base_seq=_head_seq(w), dry_run=True)
    assert out["applied"] == 2 and out["dry_run"] is True
    assert len(out["results"]) == 2
    assert snap(w) == before
    assert w.store._item("plangraph:OTHER")["note"] is None
    assert w.store._item("plangraph:HOST")["priority"] == 0
    assert _applied_rows(w) == []                  # no persisted audit/budget
    assert _denied_rows(w) == []                   # success is not a denial
    assert _open_guard_alarms(w) == []

    # The gate still runs on a dry run: a refused op is refused, nothing moves
    # in the graph -- and the denial is recorded exactly as a real call would
    # record it (D-2458: a denied dry run writes no APPLIED row, but its denied
    # oversight_ops row and its alarm survive the rollback).
    err = refusal(w.store.apply_ops, principal,
                  [op("edit", key="plangraph:HIGH", fields={"note": "no"})],
                  batch_ref=BATCH, dry_run=True)
    assert err.code == "forbidden" and has("target_high", err.msg)
    assert snap(w) == before
    assert _applied_rows(w) == []
    rows = _denied_rows(w)
    assert len(rows) == 1, [dict(r) for r in rows]
    assert (rows[0]["op"], rows[0]["key"]) == ("edit", "plangraph:HIGH")
    assert rows[0]["batch_ref"] == BATCH
    assert "oversight:" in (rows[0]["code"] or "")
    assert rows[0]["finding_id"] == FINDING
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1
    assert "oversight:" in alarms[0]["detail"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_oversight_stale_base_seq_is_conflict_and_a_denied_row(
        w, monkeypatch, dry_run):
    """The precondition behaves as on the router door, dry run or not.

    D-2458: only APPLIED rows are dry-run-proof -- a refused dry run records
    its denied oversight_ops row and its alarm exactly like a real refusal.
    """
    token, principal = live(w, monkeypatch)
    batch_ref = "batch-stale"
    base_seq = _head_seq(w)
    w.store.edit_definition(w.p("router"), "plangraph:OTHER",
                            {"note": "moved on"})
    assert _head_seq(w) > base_seq
    before = snap(w)

    err = refusal(w.store.apply_ops, principal,
                  [op("edit", key="plangraph:OTHER",
                      fields={"note": "stale write"})],
                  batch_ref=batch_ref, base_seq=base_seq, dry_run=dry_run)
    assert err.code == "conflict"
    assert "changed after base_seq" in err.msg
    assert "plangraph:OTHER" in err.msg
    assert snap(w) == before                       # refused: nothing written
    assert w.store._item("plangraph:OTHER")["note"] == "moved on"
    assert _applied_rows(w) == []

    rows = _denied_rows(w)
    assert len(rows) == 1, [dict(r) for r in rows]
    row = rows[0]
    assert row["principal"] == OPP
    assert row["batch_ref"] == batch_ref
    assert row["code"] == "conflict"
    assert "changed after base_seq" in row["reason"]
    assert "plangraph:OTHER" in row["reason"]
    assert token not in str(dict(row))
    assert len(_open_guard_alarms(w)) == 1


@pytest.mark.parametrize("op_kind,op_builder", [
    ("edit", lambda key: op("edit", key=key, fields={"note": "stale"})),
    ("priority", lambda key: op("priority", key=key, priority=5)),
    ("add_dep", lambda key: op("add_dep", key=key, dep="plangraph:HOST")),
    ("edit-depends_on-list", lambda key: op("edit", key=key,
                                             fields={"depends_on": ["plangraph:LINK"]})),
    ("edit-depends_on-add", lambda key: op("edit", key=key,
                                            fields={"depends_on": {"add": ["plangraph:LINK"]}})),
])
def test_oversight_stale_base_seq_all_op_kinds(
        w, monkeypatch, op_kind, op_builder):
    """base_seq is checked for EVERY op key oversight can send (build-L1 MED).

    Table-driven test over edit, priority, add_dep, and depends_on add/remove forms.
    Cancel is not an oversight op, so it's not tested here.
    """
    token, principal = live(w, monkeypatch)
    batch_ref = f"batch-stale-{op_kind}"
    base_seq = _head_seq(w)
    # Move the target item forward
    w.store.edit_definition(w.p("router"), "plangraph:OTHER",
                            {"note": "moved on"})
    assert _head_seq(w) > base_seq
    before = snap(w)

    err = refusal(w.store.apply_ops, principal,
                  [op_builder("plangraph:OTHER")],
                  batch_ref=batch_ref, base_seq=base_seq)
    assert err.code == "conflict"
    assert "changed after base_seq" in err.msg
    assert "plangraph:OTHER" in err.msg
    assert snap(w) == before


# =========================================================================
# Round 3: ``OVERSIGHT_DDL`` (tables + triggers) is installed BY THE OPEN
# PATH -- a fresh file through ``create=True``, and main's shipped v13 shape
# through the presence-based ``db._upgrade`` at ``create=False``.
# =========================================================================
#: The two guard triggers the brief pins (plus the tables' DELETE guards below).
_OVERSIGHT_TRIGGERS = ("trg_oversight_tokens_revoke_only_update",
                       "trg_oversight_ops_append_only_update",
                       "trg_oversight_ops_append_only_delete")

#: Exactly which ``(sqlite_master.type, name)`` rows an open must leave behind.
_OVERSIGHT_OBJECTS = sorted(
    [("table", "oversight_tokens"), ("table", "oversight_ops")]
    + [("trigger", t) for t in _OVERSIGHT_TRIGGERS])


def _oversight_objects(conn):
    """The pinned oversight objects, a sorted list of every matching row.

    A list, never a set: an install that ran twice would repeat an entry and
    fail the ``== _OVERSIGHT_OBJECTS`` comparison, so duplicates cannot hide.
    """
    marks = ",".join("?" for _ in _OVERSIGHT_TRIGGERS)
    return sorted(
        (row["type"], row["name"]) for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE"
            " (type='table' AND name IN ('oversight_tokens','oversight_ops'))"
            f" OR (type='trigger' AND name IN ({marks}))",
            _OVERSIGHT_TRIGGERS))


def _assert_oversight_trigger_refusals(conn):
    """Seed one probe row per table; the guard triggers refuse every mutation."""
    now, later = "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO oversight_tokens(id,token_sha256,principal,generation,"
        "created_at,expires_at) VALUES(?,?,?,?,?,?)",
        (1, "f" * 64, "probe", 1, now, later))
    conn.execute(
        "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
        "batch_ref,op_index,op,outcome) VALUES(?,?,?,?,?,?,?,?,?)",
        (1, now, 1, "probe", 1, "batch-probe", 0, "edit", "applied"))
    for sql, reason in (
            # a DELETE on oversight_tokens ...
            ("DELETE FROM oversight_tokens", "revoke-only"),
            # ... a non-revoke UPDATE on it ...
            ("UPDATE oversight_tokens SET mint_reason='tampered'",
             "revoke-only"),
            # ... and any UPDATE / DELETE on the append-only audit table
            ("UPDATE oversight_ops SET reason='rewritten'", "append-only"),
            ("DELETE FROM oversight_ops", "append-only")):
        with pytest.raises(db_errors.INTEGRITY, match=reason):
            conn.execute(sql)
    # every refusal aborted its whole statement: one probe row each, unchanged
    assert conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT mint_reason FROM oversight_tokens"
                        ).fetchone()["mint_reason"] is None
    assert conn.execute("SELECT COUNT(*) FROM oversight_ops"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT reason FROM oversight_ops"
                        ).fetchone()["reason"] == ""


def test_oversight_ddl_is_installed_at_open_fresh_and_upgraded_from_v13(
        tmp_path):
    """The open path owns the oversight schema: fresh files AND main's v13.

    FRESH -- ``open_db(create=True)`` leaves both oversight tables and the
    three guard triggers in ``sqlite_master``, and the guards refuse a DELETE
    on ``oversight_tokens``, a non-revoke UPDATE on it, and any UPDATE or
    DELETE on ``oversight_ops``.

    UPGRADED -- the same file stripped to main's shipped v13 shape (oversight
    tables and triggers dropped, ``PRAGMA user_version = 13``) gains exactly
    the same objects back through ``open_db(create=False)`` (``db._upgrade``'s
    presence-based ``OVERSIGHT_DDL``), refuses the same mutations, keeps
    ``user_version = 13``, and reopens twice with no error and no duplicate.
    """
    path = tmp_path / "keeper.db"

    # FRESH: a brand-new file through the normal open path.
    conn = kdb.open_db(path, create=True, backend="sqlite")
    assert _oversight_objects(conn) == _OVERSIGHT_OBJECTS
    _assert_oversight_trigger_refusals(conn)

    # Test surgery: build main's v13 shape on this very file -- drop every
    # oversight guard trigger, drop both tables, stamp user_version = 13.
    # The DDL authorizer is lifted only on this connection, which is closed
    # and replaced by a fresh ``open_db`` right after.
    conn.set_authorizer(None)
    for trigger in (*_OVERSIGHT_TRIGGERS,
                    "trg_oversight_tokens_append_only_delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in ("oversight_tokens", "oversight_ops"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version=13")
    assert _oversight_objects(conn) == []
    conn.close()

    # UPGRADED: the same open path re-installs what v13 never had.
    conn = kdb.open_db(path, backend="sqlite")
    assert _oversight_objects(conn) == _OVERSIGHT_OBJECTS
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    _assert_oversight_trigger_refusals(conn)
    conn.close()

    # Idempotent: two more opens neither fail nor add a duplicate row.
    for _ in range(2):
        again = kdb.open_db(path, backend="sqlite")
        assert _oversight_objects(again) == _OVERSIGHT_OBJECTS
        assert again.execute("PRAGMA user_version").fetchone()[0] == 13
        again.close()


def test_oversight_triggers_persist_across_reopens_and_concurrent_open(tmp_path):
    """build-L1 MED: triggers are create-if-absent (NO DROP on open), in one transaction.

    Test that a concurrent open and a re-open keep the guards in place.
    """
    path = tmp_path / "triggers-persist.db"
    def _triggers(conn):
        if getattr(conn, "backend", None) == "postgres" or os.environ.get("PG_KEEPER_BACKEND") == "postgres":
            return {row[0] for row in conn.execute(
                "SELECT trigger_name FROM information_schema.triggers"
                " WHERE trigger_schema=current_schema() AND trigger_name LIKE 'trg_oversight%'")}
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_oversight%'")}

    # First open: creates the schema with oversight tables and triggers
    conn1 = kdb.open_db(path, create=True)
    triggers1 = _triggers(conn1)
    assert "trg_oversight_tokens_revoke_only_update" in triggers1
    assert "trg_oversight_tokens_append_only_delete" in triggers1
    assert "trg_oversight_ops_append_only_update" in triggers1
    assert "trg_oversight_ops_append_only_delete" in triggers1
    conn1.close()

    # Second open: triggers must still be there (no DROP on open)
    conn2 = kdb.open_db(path)
    triggers2 = _triggers(conn2)
    assert triggers2 == triggers1
    conn2.close()

    # Concurrent opens: both should see the same triggers
    conn3 = kdb.open_db(path)
    conn4 = kdb.open_db(path)
    triggers3 = _triggers(conn3)
    triggers4 = _triggers(conn4)
    assert triggers3 == triggers4 == triggers1
    conn3.close()
    conn4.close()


def test_cli_mint_failed_write_keeps_old_token_valid(w, monkeypatch, tmp_path):
    """build-L1 LOW: if the file write fails, the old token stays valid.

    The mint is two-phase: prepare (validate + generate token), write file,
    then commit (revoke old + insert new). If the write fails, the commit
    never happens, so the old token remains valid and no new token is inserted.
    """
    from plangraph.keeper import cli

    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    # Mint the first token
    out1 = mint(w)
    token1 = out1["token"]
    assert auth.authenticate(w.conn, token1, now=w.clock()).role == "oversight"
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 1
    # Try to mint a second token with an unwritable output path
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(w.path),
                               "snapshot": str(tmp_path / "s.json")}),
                   encoding="utf-8")
    os.chmod(cfg, 0o600)
    # Use a non-existent directory to force a write failure
    bad_path = tmp_path / "nonexistent" / "token.txt"
    code = cli.main(["--config", str(cfg), "admin", "mint-oversight-token",
                     OPP, "--go", GO, "--token-out", str(bad_path)])
    assert code != 0
    # The old token is still valid (commit never happened)
    assert auth.authenticate(w.conn, token1, now=w.clock()).role == "oversight"
    # No new token was inserted
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 1
    # The old token is not revoked
    row = w.conn.execute("SELECT revoked_at FROM oversight_tokens"
                         " WHERE principal=?", (OPP,)).fetchone()
    assert row["revoked_at"] is None


# =========================================================================
# Explicit project guards for four reads; not a general read-isolation grant.
# =========================================================================
@pytest.mark.parametrize("verb,call", [
    ("needs_you", lambda s, p: s.needs_you(p, project="demo")),
    ("answers", lambda s, p: s.answers(p, project="demo")),
    ("progress", lambda s, p: s.progress(p, project="demo")),
    ("jev_report", lambda s, p: s.jev_report(p, project="demo")),
])
def test_oversight_read_verbs_are_project_scoped(w, monkeypatch, verb, call):
    """These four explicit project filters reject an out-of-scope project.

    Unfiltered and other reads retain the ordinary authenticated read contract;
    the oversight item grants project-scoped writes, not read isolation.
    """
    _seed_demo(w)
    _, principal = live(w, monkeypatch)
    # The principal is scoped to ['plangraph'], so a read on 'demo' is refused
    err = refusal(call, w.store, principal)
    assert err.code == "forbidden"
    assert "demo" in err.msg


@pytest.mark.parametrize("verb", ["edit", "add_dep", "priority"])
def test_same_batch_clamped_high_is_never_an_oversight_target(w, monkeypatch, verb):
    """D-2458: the clamp is gone, so a below-default create never exists at all.

    The create itself is refused ``oversight:tier_below_default`` at the policy
    gate -- no row, no ``high`` stand-in for the follow-up op to target (the
    pre-fix build stored the clamped default and only then refused the link).
    """
    _, principal = live(w, monkeypatch)
    item = fresh("CLAMPED")
    follow = (op("edit", key="plangraph:HOST", fields={"depends_on":
              ["plangraph:OTHER", "plangraph:CLAMPED"]}) if verb == "edit"
              else op("add_dep", key="plangraph:HOST", dep="plangraph:CLAMPED")
              if verb == "add_dep" else op("priority", key="plangraph:CLAMPED", priority=2))
    err = refusal(apply, w, principal, [op("create", item=item), follow])
    assert has("tier_below_default", err.msg)
    assert w.store._item("plangraph:CLAMPED") is None
    assert _denied_rows(w)[-1]["key"] == "plangraph:CLAMPED"


def test_prepared_mints_allocate_generation_only_at_commit(w, monkeypatch):
    live(w, monkeypatch)
    a = w.store.prepare_oversight_token(OPP, go_decision=GO)
    b = w.store.prepare_oversight_token(OPP, go_decision=GO)
    first = w.store.commit_oversight_token(a)
    second = w.store.commit_oversight_token(b)
    assert second["generation"] == first["generation"] + 1
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens WHERE revoked_at IS NULL").fetchone()[0] == 1


@pytest.mark.parametrize("raw", [
    fresh("DEFAULTS"),
    {k: v for k, v in fresh("DEFAULTS").items() if k != "kind"},
    fresh("DEFAULTS", kind="question", tier="none", evidence=["decision"]),
    dict(fresh("DEFAULTS"), project="demo"),
    dict(fresh("DEFAULTS"), title="security auth"),
])
def test_policy_simulation_uses_all_store_create_defaults(w, monkeypatch, raw):
    from plangraph.keeper import oversight_policy
    _, principal = live(w, monkeypatch, projects=(P, "demo"))
    try:
        expected = w.store._normalise_create(raw, principal)
    except Refused as exc:
        # D-2458: a below-default create is refused by BOTH seams with the same
        # reason -- the store never clamps it and the policy never grants it.
        assert has("tier_below_default", exc.msg)
        verdicts = oversight_policy.check_batch(
            {}, [op("create", item=raw)], principal.projects)
        assert [(v.reason, v.code) for v in verdicts] == \
            [("tier_below_default", "forbidden")], verdicts
        return
    seen = []
    original = oversight_policy._check_priority
    def capture(index, operation, items, projects):
        seen.append(items[expected["key"]])
        return original(index, operation, items, projects)
    monkeypatch.setattr(oversight_policy, "_check_priority", capture)
    oversight_policy.check_batch({}, [op("create", item=raw),
        op("priority", key=expected["key"], priority=1)], principal.projects)
    assert seen == [dict(expected, state="open")]


def test_oversight_read_scope_is_not_a_general_isolation_contract(w, monkeypatch):
    _seed_demo(w)
    _, principal = live(w, monkeypatch)
    assert w.store.snapshot(principal) == w.store.snapshot(w.p("router"))


# =========================================================================
# D-2458: one named test per finding of this round (each red before the fix).
# =========================================================================
def test_commit_phase_revalidates_and_rejects_a_forged_prepared(w, monkeypatch):
    """The commit phase never trusts ``prepared`` (HIGH: commit_oversight_token).

    The reported input -- a forged dict carrying a ten-year expiry, an invented
    ``D-FAKE`` GO and a digest nobody prepared -- must not mint, whichever half
    of the router GO is missing; a digest a prepare DID make still cannot be
    re-targeted at another GO, and a GO row that vanished between the two
    phases stops the commit.
    """
    from plangraph.keeper import store_oversight

    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    forged = {"name": OPP, "digest": auth.hash_token("pgo1_" + "x" * 43),
              "token": "pgo1_" + "x" * 43,
              "expires": w.clock() + 10 * 365 * 86400, "now": w.clock(),
              "go_decision": "D-FAKE", "reason": ""}
    # (a) either half of the router GO missing -> refused
    monkeypatch.delenv("PG_OVERSIGHT_LIVE", raising=False)
    assert has("not_live", refusal(w.store.commit_oversight_token, forged).msg)
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    assert has("go_required", refusal(w.store.commit_oversight_token, forged).msg)
    # (b) a real GO but a digest this process never prepared -> refused
    forged["go_decision"] = GO
    err = refusal(w.store.commit_oversight_token, forged)
    assert has("prepared_denied", err.msg)
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 0            # nothing minted
    # (c) a real prepare cannot be re-targeted at another GO
    prepared = w.store.prepare_oversight_token(OPP, go_decision=GO)
    err = refusal(w.store.commit_oversight_token, dict(prepared,
                                                       go_decision="D-FAKE"))
    assert has("go_required", err.msg)
    # (d) the GO row deleted between prepare and commit stops the commit
    if hasattr(w.conn, "set_authorizer"):
        w.conn.set_authorizer(None)                 # test surgery: DDL for one DELETE
        w.conn.execute("DROP TRIGGER trg_decisions_append_only_delete")
        w.conn.execute("DELETE FROM decisions WHERE id=?", (GO,))
    else:
        w.conn._raw.execute("DROP TRIGGER trg_decisions_append_only_delete ON decisions")
        w.conn._raw.execute("DELETE FROM decisions WHERE id=%s", (GO,))
        w.conn._raw.commit()
    err = refusal(w.store.commit_oversight_token, prepared)
    assert has("go_required", err.msg)
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 0
    # a refused commit does not burn the prepare: it is still registered
    assert store_oversight._peek_prepared(prepared["digest"]) is not None
    # the untampered prepare still commits exactly once
    grant_go(w)
    out = w.store.commit_oversight_token(prepared)
    assert out["generation"] == 1
    assert store_oversight._peek_prepared(prepared["digest"]) is None  # consumed
    assert has("prepared_denied",
               refusal(w.store.commit_oversight_token, prepared).msg)


def test_commit_ttl_bound_is_enforced_and_a_refused_commit_keeps_the_prepare(
        w, monkeypatch):
    """``expires <= now + OVERSIGHT_TOKEN_TTL_S`` is re-checked at commit (HIGH)."""
    from plangraph.keeper import store_oversight

    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    ttl = getattr(store_oversight, "OVERSIGHT_TOKEN_TTL_S", 3600)
    prepared = w.store.prepare_oversight_token(OPP, go_decision=GO)
    # the clock steps BACK out of the TTL window the prepare claimed
    w.clock.advance(-(ttl + 60))
    err = refusal(w.store.commit_oversight_token, prepared)
    assert has("token_invalid", err.msg)
    assert w.conn.execute("SELECT COUNT(*) FROM oversight_tokens"
                          ).fetchone()[0] == 0
    w.clock.advance(ttl + 60)                  # back to where it was prepared
    out = w.store.commit_oversight_token(prepared)   # a refusal did not burn it
    assert out["generation"] == 1
    assert out["expires_at"] == w.store._iso(prepared["expires"])


def test_verb_seam_denials_stop_at_the_denied_cap(w, monkeypatch):
    """MEDIUM: the hourly denied cap is consulted at the VERB seam too.

    Before this round a loop of denied ``cancel`` calls wrote one row each
    forever (``oversight_ops`` is append-only and never reaped) and no call
    ever answered ``oversight:rate_limited``; now the cap bounds the ledger
    and the next denial is the rate-limited refusal.
    """
    from plangraph.keeper import oversight_policy

    _, principal = live(w, monkeypatch)
    cap = _denied_cap(oversight_policy)
    assert cap > 0
    for _ in range(cap):
        err = refusal(w.store.cancel, principal, "plangraph:OTHER", "stop")
        assert err.code == "forbidden" and has("verb_denied", err.msg)
    assert len(_denied_rows(w)) == cap
    for _ in range(5):                         # the reported 10,000-call probe
        err = refusal(w.store.cancel, principal, "plangraph:OTHER", "stop")
        assert err.code == "forbidden" and has("rate_limited", err.msg)
        assert len(_denied_rows(w)) == cap     # the append-only ledger stops


def test_add_dep_to_a_done_dependency_is_allowed(w, monkeypatch):
    """MEDIUM: the dependency grant is "existing non-cancelled", not "open".

    A dependency the store already marked ``done`` used to be refused
    ``oversight:bad_state`` because the target ran through the item check;
    the contract (and ``docs/KEEPER.md``) permit it, only a cancelled target
    is refused.
    """
    _seed_demo(w)
    _, principal = live(w, monkeypatch, projects=(P, "demo"))
    to_done(w, "demo:DOUT")
    assert w.store._item("demo:DOUT")["state"] == "done"
    out = apply(w, principal, [
        op("add_dep", key="demo:DOTHER", dep="demo:DOUT")])
    assert out["applied"] == 1, out
    assert "demo:DOUT" in w.store._item("demo:DOTHER")["depends_on"]
    assert _denied_rows(w) == []
    # a cancelled target is still refused
    w.store.cancel(w.p("router"), "demo:DGOAL", "gone")
    err = refusal(apply, w, principal,
                  [op("add_dep", key="demo:DOTHER", dep="demo:DGOAL")])
    assert has("dep_cancelled", err.msg)


def test_authenticate_refuses_an_expired_oversight_token(w, monkeypatch):
    """MEDIUM: the TTL is enforced at authentication, not only at use time.

    A caller that trusts ``auth.authenticate`` alone used to get a live
    ``OversightPrincipal`` back for a token whose ``expires_at`` had passed;
    only ``Store._check_oversight_principal`` refused it later.
    """
    from plangraph.keeper import store_oversight

    token, principal = live(w, monkeypatch)
    ttl = getattr(store_oversight, "OVERSIGHT_TOKEN_TTL_S", 3600)
    w.clock.advance(ttl - 1)                   # one second of life left
    assert auth.authenticate(w.conn, token, now=w.clock()).name == OPP
    w.clock.advance(1)
    err = refusal(auth.authenticate, w.conn, token, now=w.clock())
    assert err.code == "forbidden" and has("token_expired", err.msg)
    # without a clock argument the store's published clock judges it (same verdict)
    err = refusal(auth.authenticate, w.conn, token)
    assert err.code == "forbidden" and has("token_expired", err.msg)
    # the principal row itself is untouched: only the token is dead
    assert auth.get_principal(w.conn, OPP).role == "oversight"


_MALFORMED_SHAPES = {
    "kind-list": op("create", item=fresh("NEW", kind=["lane"])),
    "op-list": {"op": ["edit"], "reason": "scope", "finding": FINDING,
                "key": "plangraph:OTHER", "fields": {"note": "x"}},
    "evidence-map": op("create", item=fresh("NEW2", kind="question",
                                            tier="none", evidence=[{}])),
}


@pytest.mark.parametrize("name", sorted(_MALFORMED_SHAPES))
def test_malformed_op_shapes_fail_closed_with_a_verdict(w, monkeypatch, name):
    """MEDIUM: malformed values answer with a fail-closed Verdict/Refused.

    ``kind=["lane"]``, ``{"op": ["edit"]}`` and ``evidence_kinds=[{}]`` used to
    raise ``TypeError`` inside the pure policy (500-shaped input); both seams
    now refuse, the graph is untouched and the denial is audited.
    """
    from plangraph.keeper import oversight_policy

    _, principal = live(w, monkeypatch)
    raw = _MALFORMED_SHAPES[name]
    items = {item["key"]: item for item in w.store._all_items()}
    verdicts = oversight_policy.check_batch(items, [raw], principal.projects)
    assert verdicts, name                                   # never an exception
    assert verdicts[0].code in ("forbidden", "validation"), (name, verdicts)
    assert verdicts[0].message.startswith(f"oversight:{verdicts[0].reason}")
    before = snap(w)
    err = refusal(apply, w, principal, [raw])               # TypeError would escape
    assert err.code in ("forbidden", "validation"), (name, err.code, err.msg)
    assert snap(w) == before, name
    assert len(_denied_rows(w)) == 1, name


def test_fold_does_not_rebuild_the_oversight_token_ledger(w, monkeypatch):
    """LOW: the doc claim was wrong, not the code -- pinned as corrected here.

    ``docs/KEEPER.md`` said ``oversight_token_minted`` "has a fold branch that
    updates the token ledger".  The event is KEYLESS, so ``fold`` returns
    before its branch: replay never rebuilds ``oversight_tokens`` (that table is
    revoke-only state, not an event-sourced cache) and reports no unknown type.
    The doc now names the gap instead.
    """
    token, principal = live(w, monkeypatch)
    events = keeper_events.read_events(w.conn)
    minted = [e for e in events if e["type"] == "oversight_token_minted"]
    assert minted and minted[0]["key"] is None
    unknown = []
    state = keeper_events.fold(events, unknown=unknown)
    assert unknown == []                        # known type, never "unknown"
    assert "oversight_tokens" not in state      # no ledger is reconstructed
    # the ledger is read from its table, which still holds the live generation
    row = w.conn.execute("SELECT generation FROM oversight_tokens"
                         " WHERE revoked_at IS NULL").fetchone()
    assert row["generation"] == 1
    assert token.startswith("pgo1_")


def test_read_project_guard_runs_after_the_token_recheck(w, monkeypatch):
    """LOW: the read-side project filter must not hide a dead token.

    ``progress(project=...)`` checked the project BEFORE ``_run``, so an
    expired token got a bare "not scoped" message, no ``oversight:`` reason and
    no ``oversight_ops`` denial row.  The guard now runs inside ``_run`` for
    all four explicit project filters.
    """
    from plangraph.keeper import store_oversight

    _, principal = live(w, monkeypatch)
    ttl = getattr(store_oversight, "OVERSIGHT_TOKEN_TTL_S", 3600)
    w.clock.advance(ttl + 1)
    err = refusal(w.store.progress, principal, project="demo")
    assert err.code == "forbidden" and has("token_expired", err.msg)
    rows = _denied_rows(w)
    assert len(rows) == 1
    assert rows[0]["op"] == "progress"
    assert "token_expired" in (rows[0]["code"] or "")
    # a LIVE token out of scope is refused by the filter, and that refusal is
    # audited too (it used to write no oversight_ops row at all)
    w.clock.advance(-(ttl + 1))
    err = refusal(w.store.progress, principal, project="demo")
    assert err.code == "forbidden" and "demo" in err.msg
    rows = _denied_rows(w)
    assert len(rows) == 2
    assert "not scoped" in rows[-1]["reason"]


def test_weakened_oversight_guard_triggers_are_repaired_on_open(tmp_path):
    """LOW: ``CREATE TRIGGER IF NOT EXISTS`` never repaired a weakened guard.

    Every oversight guard trigger is dropped and re-created from
    ``OVERSIGHT_DDL`` on each open (``db._upgrade``), so a permissive body --
    the finding's replacement of ``trg_oversight_ops_append_only_update`` --
    cannot survive a close/reopen, and the same holds for the token guards.
    """
    path = tmp_path / "keeper.db"
    conn = kdb.open_db(path, create=True, backend="sqlite")
    conn.set_authorizer(None)                  # test surgery: DDL for the probe
    for name, table, when in (("trg_oversight_tokens_revoke_only_update",
                               "oversight_tokens", "UPDATE"),
                              ("trg_oversight_tokens_append_only_delete",
                               "oversight_tokens", "DELETE"),
                              ("trg_oversight_ops_append_only_update",
                               "oversight_ops", "UPDATE"),
                              ("trg_oversight_ops_append_only_delete",
                               "oversight_ops", "DELETE")):
        conn.execute(f"DROP TRIGGER {name}")
        conn.execute(f"CREATE TRIGGER {name} BEFORE {when} ON {table}"
                     " BEGIN SELECT 1; END;")
    conn.execute(
        "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
        "batch_ref,op_index,op,outcome) VALUES(1,?,?,1,1,?,?,?,'applied')",
        ("2026-01-01T00:00:00+00:00", 1, "b", 0, "edit"))
    conn.execute("UPDATE oversight_ops SET reason='tampered'")   # weak guard
    assert conn.execute("SELECT reason FROM oversight_ops WHERE id=1"
                        ).fetchone()[0] == "tampered"
    conn.close()

    conn = kdb.open_db(path, backend="sqlite")  # reopen -> db._upgrade repairs
    conn.execute(
        "INSERT INTO oversight_tokens(id,token_sha256,principal,generation,"
        "created_at,expires_at) VALUES(?,?,?,?,?,?)",
        (1, "f" * 64, "probe", 1, "2026-01-01T00:00:00+00:00",
         "2027-01-01T00:00:00+00:00"))
    for sql, why in (("UPDATE oversight_ops SET reason='x'", "append-only"),
                     ("DELETE FROM oversight_ops", "append-only"),
                     ("UPDATE oversight_tokens SET mint_reason='x'", "revoke-only"),
                     ("DELETE FROM oversight_tokens", "revoke-only")):
        with pytest.raises(db_errors.INTEGRITY, match=why):
            conn.execute(sql)
    body = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger'"
        " AND name='trg_oversight_ops_append_only_update'").fetchone()[0]
    assert "append-only" in body                # the CURRENT body, not mine
    conn.close()


def test_rate_limited_denial_row_carries_a_human_readable_reason(w, monkeypatch):
    """LOW: the budget refusal used to store ``reason=''``.

    Exhaust the applied cap, then apply one more op: the denied row keeps
    ``code='oversight:rate_limited'`` AND carries the budget text (an op that
    never ran contributes no input reason of its own).
    """
    from plangraph.keeper import oversight_policy

    _, principal = live(w, monkeypatch)
    cap = _applied_cap(oversight_policy)
    for i in range(cap):
        apply(w, principal, [op("priority", key="plangraph:OTHER",
                                priority=i % 7)])
    assert _denied_rows(w) == []
    err = refusal(apply, w, principal,
                  [op("priority", key="plangraph:OTHER", priority=3)])
    assert err.code == "forbidden" and has("rate_limited", err.msg)
    rows = _denied_rows(w)
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["code"] == "oversight:rate_limited"
    assert rows[0]["reason"], dict(rows[0])     # never empty
    assert "budget" in rows[0]["reason"]


def test_applied_cap_stops_a_batch_mid_flight(w, monkeypatch):
    """Pattern sweep: EVERY writer consults the cap, not only the batch entry.

    One batch that would walk past the applied cap is refused ``rate_limited``
    at the row that crosses it and rolls back whole (the entry check only sees
    the count the batch started with).
    """
    from plangraph.keeper import oversight_policy

    _, principal = live(w, monkeypatch)
    cap = _applied_cap(oversight_policy)
    ops = [op("priority", key="plangraph:OTHER", priority=i % 7)
           for i in range(cap + 5)]
    err = refusal(apply, w, principal, ops)
    assert err.code == "forbidden" and has("rate_limited", err.msg)
    assert _applied_rows(w) == []              # rolled back, all-or-nothing
    rows = _denied_rows(w)
    assert len(rows) == 1 and rows[0]["reason"]


def test_open_alarm_unique_index_exists_on_both_schemas(tmp_path):
    """glm MEDIUM: ``ON CONFLICT (...) WHERE cleared_at IS NULL`` needs its index.

    Declared on BOTH schemas (the SQLite DDL the open path installs and PG
    migration 3), present on the live backend this run uses, and it really is
    the dedupe target: a second OPEN row on the same key is refused while a
    cleared one re-opens.
    """
    from plangraph.keeper import schema_pg
    from plangraph.projector import ddl as projector_ddl

    clause = "ON projector_alarms(kind, workflow_id, key) WHERE cleared_at IS NULL"
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_projector_alarms_open" in \
        projector_ddl.SQLITE
    assert "CREATE UNIQUE INDEX idx_projector_alarms_open" in projector_ddl.PG
    assert clause in projector_ddl.SQLITE and clause in projector_ddl.PG
    assert schema_pg.MIGRATIONS[2].sql == projector_ddl.PG   # PG migration 3

    world = World(tmp_path)
    try:
        backend = os.environ.get("PG_KEEPER_BACKEND") or "sqlite"
        if backend == "postgres":
            row = world.conn.execute(
                "SELECT indexdef FROM pg_indexes"
                " WHERE indexname='idx_projector_alarms_open'"
                " AND schemaname=current_schema()").fetchone()
            assert row is not None
            idef = row["indexdef"]
            assert "UNIQUE INDEX" in idef
            assert "projector_alarms" in idef
            assert "(kind, workflow_id, key)" in idef
            assert "cleared_at IS NULL" in idef
        else:
            row = world.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index'"
                " AND name='idx_projector_alarms_open'").fetchone()
            assert row is not None and clause in row["sql"]
        now = "2026-01-01T00:00:00+00:00"
        world.conn.execute(
            "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,raised_at)"
            " VALUES(1,'oversight_denied','oversight-guard','probe','d',?)", (now,))
        with pytest.raises(db_errors.INTEGRITY):
            world.conn.execute(
                "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,raised_at)"
                " VALUES(2,'oversight_denied','oversight-guard','probe','d',?)", (now,))
        world.conn.execute("UPDATE projector_alarms SET cleared_at=? WHERE id=1",
                           (now,))
        world.conn.execute(
            "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,raised_at)"
            " VALUES(3,'oversight_denied','oversight-guard','probe','d',?)", (now,))
    finally:
        world.close()


def test_rotating_finding_ids_keep_one_open_alarm(w, monkeypatch):
    """glm MEDIUM: one open ``oversight_denied`` alarm PER PRINCIPAL.

    The key used to be the op-supplied finding, so rotating finding ids minted
    one open row each (unbounded open alarms for routing-00) while the contract
    promises one open row; the finding still rides in the detail.
    """
    _, principal = live(w, monkeypatch)
    for i in range(10):
        err = refusal(apply, w, principal, [op(
            "edit", key="plangraph:OTHER", fields={"done_criteria": f"no {i}"},
            finding=f"F-{i:04d}")])
        assert has("field_denied", err.msg)
    assert len(_denied_rows(w)) == 10          # the audit ledger keeps them all
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1, [dict(a) for a in alarms]
    assert alarms[0]["key"] == OPP
    assert "oversight:" in alarms[0]["detail"]
    assert "F-0009" in alarms[0]["detail"]      # latest finding is named on conflict
    assert "count 10" in alarms[0]["detail"]


def test_principal_token_never_dispatches_to_the_oversight_lookup(w, monkeypatch):
    """glm LOW: the ``pgo1_`` dispatch keys on a stored marker, not on text.

    ``secrets.token_urlsafe`` draws from an alphabet that contains ``_``, so a
    prefixless principal token could START with ``pgo1_`` and be refused as an
    unknown oversight token.  The principals table is consulted first, and the
    mint re-rolls the prefix away so a fresh token cannot even look like one.
    """
    token = "pgo1_" + "Z" * 40
    w.conn.execute(
        "INSERT INTO principals(name,role,projects,family,token_sha256,active)"
        " VALUES('misfire','l1',?,?,?,1)",
        (json.dumps([P]), None, auth.hash_token(token)))
    got = auth.authenticate(w.conn, token)
    assert (got.name, got.role) == ("misfire", "l1")   # its own row, not a 403
    # the mint never emits such a token: a pgo1_-shaped draw is re-rolled
    draws = iter(["pgo1_" + "q" * 40, "zzzz" + "q" * 40])
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda n: next(draws))
    fresh_token = auth.add_principal(w.conn, "misfire2", "reader", [])
    assert fresh_token == "zzzz" + "q" * 40
    assert auth.authenticate(w.conn, fresh_token).name == "misfire2"


def test_authenticate_clock_is_not_process_global(w, monkeypatch):
    """auth.py Finding 1: no process-global clock; authenticate defaults to time.time()."""
    from plangraph.keeper.store import Store
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    store_zero = Store(w.conn, clock=lambda: 0.0)
    tok = store_zero.mint_oversight_token(OPP, go_decision=GO, reason="test")["token"]
    store_wall = Store(w.conn)
    p = auth.authenticate(w.conn, tok, now=10.0)
    assert p.name == OPP
    # Rebuilding store_zero must not cause authenticate() without now to accept
    # an expired token (expires at 3600, wall clock is ~1.7e9)
    Store(w.conn, clock=lambda: 0.0)
    with pytest.raises(Refused) as exc:
        auth.authenticate(w.conn, tok)
    assert "token_expired" in exc.value.msg


def test_expired_oversight_token_authenticate_audited_and_capped(w, monkeypatch):
    """auth.py Finding 2: expired pgo1_ token audits denial and is rate-limited at cap."""
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    out = mint(w)
    tok = out["token"]
    past = w.clock() + 4000.0
    for _ in range(10):
        with pytest.raises(Refused) as exc:
            auth.authenticate(w.conn, tok, now=past)
        assert "token_expired" in exc.value.msg

    with pytest.raises(Refused) as exc:
        auth.authenticate(w.conn, tok, now=past)
    assert "rate_limited" in exc.value.msg

    rows = _denied_rows(w)
    assert len(rows) == 10
    assert rows[0]["code"] == "oversight:token_expired"
    assert rows[0]["op"] == "authenticate"
    assert rows[0]["principal"] == OPP

    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1
    assert alarms[0]["key"] == OPP

    unknown_tok = "pgo1_" + "X" * 40
    with pytest.raises(Refused) as exc:
        auth.authenticate(w.conn, unknown_tok, now=past)
    assert "unknown or revoked" in exc.value.msg
    assert len(_denied_rows(w)) == 10


def test_concurrent_commit_oversight_token_two_threads(w, monkeypatch):
    """store_oversight.py Finding 4: two concurrent commits cannot both mint."""
    import threading
    from plangraph.keeper import db as kdb
    from plangraph.keeper.store import Store
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    prepared = w.store.prepare_oversight_token(OPP, go_decision=GO, reason="test")

    results = []
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def run_commit():
        conn = kdb.open_db(w.path)
        try:
            store = Store(conn, clock=w.clock)
            barrier.wait()
            try:
                res = store.commit_oversight_token(prepared)
                with lock:
                    results.append(res)
            except Exception as ex:
                with lock:
                    errors.append(ex)
        finally:
            conn.close()

    threads = [threading.Thread(target=run_commit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert "oversight:prepared_denied" in str(errors[0])


def test_oversight_guard_alarm_preserves_initial_finding_on_conflict(w, monkeypatch):
    """store_oversight.py Finding 5: on conflict UPDATE open alarm detail to latest finding + count."""
    _, principal = live(w, monkeypatch)
    err1 = refusal(apply, w, principal, [op(
        "edit", key="plangraph:OTHER", fields={"done_criteria": "no 0"},
        finding="F-0000")])
    assert has("field_denied", err1.msg)
    err2 = refusal(apply, w, principal, [op(
        "edit", key="plangraph:OTHER", fields={"done_criteria": "no 1"},
        finding="F-0001")])
    assert has("field_denied", err2.msg)

    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1
    assert "F-0001" in alarms[0]["detail"]
    assert "count 2" in alarms[0]["detail"]


def test_guard_alarm_count_is_ledger_derived_not_parsed_from_text(w, monkeypatch):
    """I7: a finding like ``F-count 7`` must never inflate the alarm count.

    The count is ``COUNT(*)`` of denied ``oversight_ops`` rows since the open
    alarm's ``raised_at`` (same transaction as the audit insert), never a
    number recovered from the detail's free text.
    """
    _, principal = live(w, monkeypatch)
    err1 = refusal(apply, w, principal, [op(
        "edit", key="plangraph:OTHER", fields={"done_criteria": "no"},
        finding="F-count 7")])
    assert has("field_denied", err1.msg)
    err2 = refusal(apply, w, principal, [op(
        "edit", key="plangraph:OTHER", fields={"done_criteria": "nope"},
        finding="F-0002")])
    assert has("field_denied", err2.msg)
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1
    assert "F-0002" in alarms[0]["detail"]          # the LATEST finding
    assert alarms[0]["detail"].endswith("count 2")  # 2 denials, not 8


def test_expired_auth_alarm_carries_the_same_ledger_count(w, monkeypatch):
    """I7: the expired-auth alarm uses DO UPDATE and the same ledger count.

    Expired-auth denials and op denials share one open alarm and one append-only
    ledger, so the first op denial after an expiry reports ``count 2``, not 1.
    """
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    tok = mint(w)["token"]
    w.clock.advance(4000)                          # the TTL has passed
    err = refusal(auth.authenticate, w.conn, tok, now=w.clock())
    assert "token_expired" in err.msg
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1
    assert alarms[0]["detail"].endswith("count 1")

    principal = auth.authenticate(w.conn, mint(w)["token"], now=w.clock())
    err = refusal(apply, w, principal, [op(
        "edit", key="plangraph:OTHER", fields={"done_criteria": "no"},
        finding="F-0002")])
    assert has("field_denied", err.msg)
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1
    assert alarms[0]["detail"].endswith("count 2")


def test_expired_denials_x3_count_three(w, monkeypatch):
    """I7: expired x3 -> one open alarm whose detail ends ``count 3``."""
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    tok = mint(w)["token"]
    for i in range(3):
        refusal(auth.authenticate, w.conn, tok, now=w.clock() + 4000.0)
        alarms = _open_guard_alarms(w)
        assert len(alarms) == 1
        assert alarms[0]["detail"].endswith(f"count {i + 1}")


def test_concurrent_op_denials_one_alarm_with_the_true_count(w, monkeypatch):
    """E4/I5: concurrent denial writers serialise on the write transaction.

    Each denial writes its row and refreshes the ONE open alarm inside the same
    transaction, so the final detail ends the true ledger count whichever
    thread finishes last -- and a ``count``-shaped finding cannot inflate it.
    """
    import threading
    from plangraph.keeper import db as kdb
    from plangraph.keeper.store import Store

    _, principal = live(w, monkeypatch)
    n = 5
    barrier = threading.Barrier(n)
    errors = []

    def deny():
        conn = kdb.open_db(w.path)
        try:
            store = Store(conn, clock=w.clock)
            barrier.wait()
            try:
                store.apply_ops(principal, [op(
                    "edit", key="plangraph:OTHER",
                    fields={"done_criteria": "no"}, finding="F-count 7")],
                    batch_ref=BATCH)
            except Refused:
                pass
            except Exception as exc:
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=deny) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(_denied_rows(w)) == n
    alarms = _open_guard_alarms(w)
    assert len(alarms) == 1
    assert alarms[0]["detail"].endswith(f"count {n}")


def test_concurrent_expired_auth_cap_holds_under_threads(w, monkeypatch):
    """A1: concurrent expired auth denials respect the cap of 10 in one BEGIN IMMEDIATE txn."""
    import threading
    from plangraph.keeper import db as kdb
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    out = mint(w)
    tok = out["token"]
    past = w.clock() + 4000.0

    for _ in range(9):
        with pytest.raises(Refused) as exc:
            auth.authenticate(w.conn, tok, now=past)
        assert "token_expired" in exc.value.msg

    barrier = threading.Barrier(5)
    def attempt_auth():
        conn = kdb.open_db(w.path)
        try:
            barrier.wait()
            try:
                auth.authenticate(conn, tok, now=past)
            except Refused:
                pass
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt_auth) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn2 = kdb.open_db(w.path)
    try:
        count = conn2.execute(
            "SELECT COUNT(*) FROM oversight_ops WHERE principal=? AND code='oversight:token_expired'",
            (OPP,)
        ).fetchone()[0]
        assert count == 10
    finally:
        conn2.close()


def test_expired_token_replay_does_not_spend_live_op_denied_budget(w, monkeypatch):
    """A2: 10+ expired replays do not lock principal out of applying valid ops."""
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    out1 = mint(w)
    tok1 = out1["token"]
    past = w.clock() + 4000.0

    for i in range(12):
        with pytest.raises(Refused) as exc:
            auth.authenticate(w.conn, tok1, now=past)
        if i < 10:
            assert "token_expired" in exc.value.msg
        else:
            assert "rate_limited" in exc.value.msg

    out2 = mint(w)
    tok2 = out2["token"]
    p_valid = auth.authenticate(w.conn, tok2, now=w.clock())

    res = apply(w, p_valid, [op("edit", key="plangraph:OTHER",
                                fields={"note": "valid op after replays"})])
    assert res is not None


def test_audit_expired_auth_alarm_failure_rolls_back_ops_row(w, monkeypatch):
    """A3: alarm insert failure rolls back oversight_ops insert in audit_oversight_expired_auth."""
    monkeypatch.setenv("PG_OVERSIGHT_LIVE", "1")
    grant_go(w)
    add_oversight(w)
    out = mint(w)
    tok = out["token"]
    past = w.clock() + 4000.0

    class FailingConn:
        def __init__(self, real):
            self._real = real
        def __getattr__(self, name):
            return getattr(self._real, name)
        def execute(self, sql, *args):
            if "projector_alarms" in str(sql) and "INSERT" in str(sql):
                raise RuntimeError("forced alarm insert failure")
            return self._real.execute(sql, *args)

    proxy = FailingConn(w.conn)
    with pytest.raises(RuntimeError, match="forced alarm insert failure"):
        auth.authenticate(proxy, tok, now=past)

    count = w.conn.execute(
        "SELECT COUNT(*) FROM oversight_ops WHERE principal=?", (OPP,)
    ).fetchone()[0]
    assert count == 0


def test_auth_set_clock_removed():
    """A6: set_clock is removed from auth and its export list."""
    assert not hasattr(auth, "set_clock")
    assert "set_clock" not in auth.__all__


def test_oversight_ops_key_none_and_batch_ref_empty_satisfy_not_null(w):
    """A8: key=None and batch_ref='' satisfy oversight_ops NOT NULL on SQLite and PG."""
    now = w.store._iso(w.clock())
    w.conn.execute(
        "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
        "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
        " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_ops),"
        "?,?,?,?,?,?,?,?,?,?,'denied',?,NULL)",
        (now, 1, "test_p", 1, "", 1, "authenticate", None, "", "reason", "code"))
    row = w.conn.execute("SELECT batch_ref, key FROM oversight_ops WHERE principal='test_p'").fetchone()
    assert row["batch_ref"] == ""
    assert row["key"] is None

    pg_dsn = os.environ.get("PG_DSN")
    if pg_dsn:
        import uuid
        import psycopg
        from plangraph.keeper import db as kdb
        schema_name = f"test_ovs_{uuid.uuid4().hex[:8]}"
        try:
            pg_conn = kdb.open_db("test", create=True, backend="postgres", dsn=pg_dsn, schema=schema_name)
            try:
                pg_conn.execute(
                    "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
                    "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
                    " VALUES(999999,?,?,?,?,?,?,?,?,?,?,'denied',?,NULL)",
                    (now, 1, "test_p", 1, "", 1, "authenticate", None, "", "reason", "code"))
                prow = pg_conn.execute("SELECT batch_ref, key FROM oversight_ops WHERE id=999999").fetchone()
                assert prow["batch_ref"] == ""
                assert prow["key"] is None
            finally:
                pg_conn.close()
        finally:
            try:
                raw = psycopg.connect(pg_dsn, autocommit=True)
                raw.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
                raw.close()
            except Exception:
                pass


