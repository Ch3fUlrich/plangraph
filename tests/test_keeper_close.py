"""X7b: tier ``none`` and the router-only ``close_on_decision`` verb.

A decision-only item (``review_tier='none'``, ``evidence_kinds=['decision']``)
never gets reviewed: the router closes it straight from ``claimed``/``in_review``
to ``done`` on a verified decision plus a hashed batch file inside a configured
``batch_roots`` directory.  The resulting ``closed_on_decision`` event locks the
key exactly like ``review_passed`` does.
"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, seed, to_claimed, to_in_review
from plangraph.keeper import config
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

KEY = "demo:A"
DID = "D-300"


def _decisions(tmp_path, body):
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Scope |\n|---|---|---|\n" + body,
                    encoding="utf-8")
    return path


def _batch(tmp_path, text="batch body\n", name="batch.md", subdir="plan"):
    """Write a batch file under ``tmp_path/plan``; return (path, batch_ref)."""
    root = tmp_path / subdir
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, f"{path}@{digest}", digest


def _seed(w, tier="none", kinds=("decision",)):
    """A tier-``none`` lane plus an ordinary goal (``keeper_util.seed`` gives
    the goal the same definition kwargs, which ``none`` forbids)."""
    w.store.create_items(w.p("router"), [
        defn("A", [], evidence_kinds=list(kinds), review_tier=tier),
        defn("GOAL", ["A"], kind="goal"),
    ])


def _world(tmp_path, decisions_body, *, tier="none", kinds=("decision",),
           roots=None, attach=True, active=True):
    w = World(tmp_path)
    _seed(w, tier=tier, kinds=kinds)
    path = _decisions(tmp_path, decisions_body)
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    _, ref, _ = _batch(tmp_path)
    w.batch_ref = ref
    w.store.batch_roots = [str(tmp_path / "plan")] if roots is None else roots
    if active:
        to_claimed(w, KEY)
    if attach:
        w.store.add_evidence_router(w.p("router"), KEY, "decision",
                                    {"id": DID})
    return w


CLOSES = f"| {DID} | legacy batch close | Closes: demo:A |\n"
CLOSES_OTHER = f"| {DID} | legacy batch close | Closes: demo:Z |\n"
NO_CLOSES = f"| {DID} | recorded, no Closes token |\n"


# ------------------------------------------------------- tier `none` at create
def test_tier_none_needs_decision_only_evidence(tmp_path):
    w = World(tmp_path)
    _seed(w)
    assert w.store._item("demo:A")["review_tier"] == "none"
    assert w.store._item("demo:GOAL")["review_tier"] == "standard"
    w.close()


def test_create_tier_none_wrong_evidence_kinds(tmp_path):
    w = World(tmp_path)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("A", [], review_tier="none",
                                                  evidence_kinds=["any"])])
    assert exc.value.code == "validation"
    with pytest.raises(Refused):
        w.store.create_items(w.p("router"), [defn(
            "B", [], review_tier="none",
            evidence_kinds=["decision", "commit+ci"])])
    w.close()


def test_create_tier_none_needs_router(tmp_path):
    w = World(tmp_path)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("l1main"), [defn(
            "A", [], review_tier="none", evidence_kinds=["decision"])])
    assert exc.value.code == "forbidden"
    w.close()


def test_edit_tier_none_guards_kind_and_role(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), KEY, {"review_tier": "none"})
    assert exc.value.code == "validation"
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), KEY, {"review_tier": "none",
                                                    "evidence_kinds":
                                                        ["decision"]})
    assert exc.value.code == "forbidden"
    w.store.edit_definition(w.p("router"), KEY,
                            {"review_tier": "none",
                             "evidence_kinds": ["decision"]})
    assert w.store._item(KEY)["review_tier"] == "none"
    w.close()


def test_tier_none_rejects_unknown_tier_name(tmp_path):
    w = World(tmp_path)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("A", [], review_tier="loose")])
    assert exc.value.code == "validation"
    w.close()


# --------------------------------------------------------------- happy path
def test_close_marks_done_and_locked(tmp_path):
    w = _world(tmp_path, CLOSES)
    out = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert out["state"] == "done"
    item = w.store._item(KEY)
    assert item["state"] == "done"
    row = w.conn.execute("SELECT type,actor,payload FROM events WHERE key=?"
                         " AND type='closed_on_decision'", (KEY,)).fetchone()
    assert row["actor"] == "router"
    payload = json.loads(row["payload"])
    assert payload["decision_ids"] == [DID]
    assert payload["sha256"] == w.batch_ref.rsplit("@", 1)[1]
    w.close()


def test_close_in_review_state_ok(tmp_path):
    w = _world(tmp_path, CLOSES + "| D-1 | plain row |\n", active=False,
               attach=False)
    epoch = to_in_review(w, KEY)
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    assert w.store._item(KEY)["state"] == "in_review"
    out = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert out["state"] == "done"
    w.close()


@pytest.mark.parametrize("verb", ["edit", "cancel"])
def test_closed_item_is_locked(tmp_path, verb):
    w = _world(tmp_path, CLOSES)
    w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    with pytest.raises(Refused) as exc:
        if verb == "edit":
            w.store.edit_definition(w.p("router"), KEY, {"title": "later"})
        else:
            w.store.cancel(w.p("router"), KEY, "too late")
    assert exc.value.code == "locked"
    with pytest.raises(Refused):
        w.store.add_evidence_router(w.p("router"), KEY, "decision",
                                    {"id": "D-301"})
    w.close()


def test_fold_and_verify_integrity_agree(tmp_path):
    w = _world(tmp_path, CLOSES)
    w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    folded = keeper_events.fold(keeper_events.read_events(w.conn))[KEY]
    assert folded["state"] == "done"
    assert folded["reviewer"] == "router"
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []
    w.close()


# ---------------------------------------------------------------- refusals
def _refuse(w, code, batch=None, principal="router", key=KEY):
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p(principal), key,
                                  batch if batch is not None else w.batch_ref)
    assert exc.value.code == code, f"{exc.value.code} != {code}: {exc.value.msg}"
    return exc.value


def test_unverified_decision_refused(tmp_path):
    w = _world(tmp_path, CLOSES)
    w.verifiers["decision"].ok = False
    _refuse(w, "evidence_unverified")
    assert w.store._item(KEY)["state"] == "claimed"


def test_decision_absent_from_file_refused(tmp_path):
    """The row is attached but the decisions file never recorded it."""
    w = _world(tmp_path, NO_CLOSES, attach=False)
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-999"})
    _refuse(w, "evidence_unverified")


def test_no_decision_evidence_refused(tmp_path):
    w = _world(tmp_path, CLOSES, attach=False)
    _refuse(w, "evidence_missing")


def test_wrong_sha_refused(tmp_path):
    w = _world(tmp_path, CLOSES)
    path, digest = Path(w.batch_ref.rsplit("@", 1)[0]), \
        w.batch_ref.rsplit("@", 1)[1]
    _refuse(w, "evidence_unverified", batch=f"{path}@{'0' * 64}")
    assert digest != "0" * 64


def test_batch_file_edited_after_ref_refused(tmp_path):
    w = _world(tmp_path, CLOSES)
    path = Path(w.batch_ref.rsplit("@", 1)[0])
    path.write_text("tampered\n", encoding="utf-8")
    _refuse(w, "evidence_unverified")


def test_unreadable_batch_file_refused(tmp_path):
    w = _world(tmp_path, CLOSES)
    _refuse(w, "evidence_unverified",
            batch=str(tmp_path / "plan" / "gone.md") + "@" + "a" * 64)


def test_malformed_batch_ref_refused(tmp_path):
    w = _world(tmp_path, CLOSES)
    for bad in ["", "no-at-sign", "/x@abc", "@n"] :
        _refuse(w, "validation", batch=bad)


@pytest.mark.parametrize("suffix", ["../secret.md",
                                    "../../outside.md",
                                    "subdir/../../outside.md"])
def test_path_traversal_outside_batch_roots_refused(tmp_path, suffix):
    w = _world(tmp_path, CLOSES)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (tmp_path / "secret.md").write_text("secret\n", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    ref = str(Path(str(tmp_path / "plan")) / suffix) + "@" + digest
    exc = _refuse(w, "forbidden", batch=ref)
    assert "batch_roots" in exc.msg


def test_symlink_out_of_batch_roots_refused(tmp_path):
    w = _world(tmp_path, CLOSES)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "real.md"
    target.write_text("sneaky\n", encoding="utf-8")
    link = tmp_path / "plan" / "link.md"
    os.symlink(str(target), str(link))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    exc = _refuse(w, "forbidden", batch=f"{link}@{digest}")
    assert "batch_roots" in exc.msg


def test_empty_batch_roots_always_refused(tmp_path):
    w = _world(tmp_path, CLOSES, roots=[])
    _refuse(w, "forbidden")


def test_blank_batch_root_does_not_mean_cwd(tmp_path, monkeypatch):
    """``realpath("")`` is the cwd; a blank entry must not authorise it."""
    w = _world(tmp_path, CLOSES, roots=[""])
    monkeypatch.chdir(tmp_path)
    _refuse(w, "forbidden")


def test_closes_present_but_key_missing_refused(tmp_path):
    w = _world(tmp_path, CLOSES_OTHER)
    exc = _refuse(w, "evidence_unverified")
    assert "demo:A" in exc.msg


def test_closes_lists_key_ok(tmp_path):
    w = _world(tmp_path, f"| {DID} | close | Closes: demo:Z, demo:A |\n")
    assert w.store.close_on_decision(w.p("router"), KEY,
                                     w.batch_ref)["state"] == "done"
    w.close()


def test_no_closes_token_authorised_by_batch_ref_only(tmp_path):
    w = _world(tmp_path, NO_CLOSES)
    assert w.store.close_on_decision(w.p("router"), KEY,
                                     w.batch_ref)["state"] == "done"
    w.close()


def test_non_router_forbidden(tmp_path):
    w = _world(tmp_path, CLOSES)
    for who in ("l1main", "workerA", "reviewer", "reader", "system"):
        _refuse(w, "forbidden", principal=who)


def test_open_item_must_be_moved_first(tmp_path):
    w = _world(tmp_path, CLOSES, active=False, attach=False)
    assert w.store._item(KEY)["state"] == "open"
    _refuse(w, "bad_state")


@pytest.mark.parametrize("tier,kinds", [("standard", ("any",)),
                                        ("high", ("decision",))])
def test_tier_not_none_refused(tmp_path, tier, kinds):
    w = _world(tmp_path, CLOSES, tier=tier, kinds=kinds)
    exc = _refuse(w, "bad_state")
    assert tier in exc.msg


def test_done_item_reports_locked(tmp_path):
    w = _world(tmp_path, CLOSES)
    w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    _refuse(w, "locked")


def test_unknown_key_not_found(tmp_path):
    w = _world(tmp_path, CLOSES)
    _refuse(w, "not_found", key="demo:NOPE")


def test_closed_on_decision_is_a_known_event_type():
    from plangraph.keeper import schema
    assert "closed_on_decision" in schema.EVENT_TYPES
    assert "closed_on_decision" in schema.TRIGGERS


def test_close_locks_the_events_table_at_sql_level(tmp_path):
    """The lock is the trigger's, not just a Python check."""
    import sqlite3
    w = _world(tmp_path, CLOSES)
    w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        keeper_events.append_event(w.conn, "2026-01-01T00:00:00+00:00", "router",
                                   "router", None, KEY, "cancelled", {})
    assert "locked" in str(exc.value)
    w.conn.rollback()
    w.close()


# ------------------------------------------------------------------- config
def _cfg(tmp_path, extra):
    path = tmp_path / "keeper.json"
    data = {"db": "keeper.db", "bind": "127.0.0.1:8765"}
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(str(path), 0o600)
    return config.load_config(path)


def test_config_batch_roots_default_empty(tmp_path):
    assert _cfg(tmp_path, {}).batch_roots == []


def test_config_batch_roots_resolved(tmp_path):
    cfg = _cfg(tmp_path, {"batch_roots": ["/srv/plan", "rel/plan"]})
    assert cfg.batch_roots == ["/srv/plan", str(tmp_path / "rel/plan")]


def test_config_batch_roots_must_be_a_list(tmp_path):
    with pytest.raises(config.ConfigError):
        _cfg(tmp_path, {"batch_roots": "/srv/plan"})
