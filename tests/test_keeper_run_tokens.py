"""M2B-RUN-TOKENS: mint, bind, replay, expiry, revocation and the attack cases."""
import json

import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity

RUNS = {"prefix": "demo:AB", "w1": "demo:A", "w2": "demo:A", "rv1": "demo:A", "rv2": "demo:A",
        "other": "demo:B", "nofam": "demo:A"}
FAMILY = {"rv1": "teamC", "rv2": "teamR"}


class FakeRuns:
    def valid_run_id(self, run_id):
        return isinstance(run_id, str) and run_id.replace("-", "").isalnum()

    def job(self, run_id):
        if run_id not in RUNS:
            raise FileNotFoundError(run_id)
        req = {"title": f"work on {RUNS[run_id]}"}
        if run_id in FAMILY:
            req["family"] = FAMILY[run_id]
        return {"request": req, "task": ""}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", [])])
    world.store.ctx = {"runs": FakeRuns()}
    yield world
    world.close()


def mint(w, who, key, role, run):
    return w.store.mint_run_token(w.p(who), key, role, run)


def rp(w, token):
    return auth.authenticate(w.conn, token)


def code(fn, *a):
    with pytest.raises(Refused) as exc:
        fn(*a)
    return exc.value.code


def claimed_with_token(w, run="w1", key="demo:A", who="l1main"):
    epoch = to_claimed(w, key, who)
    out = mint(w, who, key, "writer", run)
    return epoch, out["token"], rp(w, out["token"])


def to_review(w, key="demo:A"):
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), key, epoch)
    return epoch


def revoked(w, token):
    row = w.conn.execute("SELECT revoked_at FROM run_tokens WHERE token_sha256=?",
                         (auth.hash_token(token),)).fetchone()
    return row["revoked_at"] is not None


# ------------------------------------------------------------------ mint
def test_mint_stores_only_hash_and_binds(w):
    epoch = to_claimed(w, "demo:A")
    out = mint(w, "l1main", "demo:A", "writer", "w1")
    tok = out["token"]
    row = w.conn.execute("SELECT * FROM run_tokens").fetchone()
    assert row["token_sha256"] == auth.hash_token(tok) and tok not in row.keys()
    assert (row["key"], row["epoch"], row["role"], row["run_id"]) == (
        "demo:A", epoch, "writer", "w1")
    lease = w.store._item("demo:A")["lease_until"]
    assert row["expires_at"] <= lease and out["expires_at"] == row["expires_at"]
    assert verify_integrity(w.conn) == []
    ev = w.conn.execute("SELECT payload FROM events WHERE type='token_minted'"
                        ).fetchone()
    assert tok not in ev["payload"] and json.loads(ev["payload"])["run_id"] == "w1"


def test_token_never_in_db_text(w):
    _, tok, p = claimed_with_token(w)
    w.store.heartbeat(p, "demo:A", p.epoch)
    code(w.store.mint_run_token, p, "demo:A", "writer", "w1")
    dump = "\n".join(str(tuple(r)) for t in ("events", "audit_log", "audit_findings",
                                            "evidence", "items", "principals")
                     for r in w.conn.execute(f"SELECT * FROM {t}"))
    assert tok not in dump
    assert tok not in str(w.store.show(w.p("router"), "demo:A"))
    assert tok not in str(w.store.snapshot(w.p("router")))


@pytest.mark.parametrize("who", ["workerA", "reviewer", "reader", "l1other",
                                 "system"])
def test_non_minters_refused(w, who):
    to_claimed(w, "demo:A")
    assert code(mint, w, who, "demo:A", "writer", "w1") == "forbidden"


def test_router_and_own_l1_may_mint(w):
    to_claimed(w, "demo:A")
    assert mint(w, "router", "demo:A", "writer", "w1")["token"]
    assert mint(w, "l1main", "demo:A", "writer", "w2")["token"]


def test_run_token_cannot_mint(w):
    _, _, p = claimed_with_token(w)
    assert code(w.store.mint_run_token, p, "demo:A", "writer", "w2") == "forbidden"
    assert code(w.store.mint_run_token, p, "demo:A", "reviewer", "rv1") == "forbidden"


@pytest.mark.parametrize("role,run,state_ok,expect", [
    ("writer", "nope", True, "evidence_unverified"),
    ("writer", "other", True, "evidence_unverified"),   # run is for demo:B
    ("writer", "prefix", True, "evidence_unverified"),  # demo:AB is not demo:A
    ("writer", "bad id!", True, "validation"),
    ("owner", "w1", True, "validation"),
    ("reviewer", "rv1", True, "bad_state"),             # still claimed
    ("writer", "w1", False, "bad_state"),               # open
])
def test_mint_refusals(w, role, run, state_ok, expect):
    if state_ok:
        to_claimed(w, "demo:A")
    assert code(mint, w, "l1main", "demo:A", role, run) == expect


def test_mint_malformed_job_refused(w):
    to_claimed(w, "demo:A")
    w.store.ctx = {"runs": type("R", (), {"valid_run_id": lambda s, r: True,
                                          "job": lambda s, r: ["not", "a", "dict"]})()}
    assert code(mint, w, "l1main", "demo:A", "writer", "w1") == "evidence_unverified"


def test_mint_without_run_record_refused(w):
    to_claimed(w, "demo:A")
    w.store.ctx = None
    assert code(mint, w, "l1main", "demo:A", "writer", "w1") == "evidence_unverified"


def test_no_principal_named_run(w):
    assert code(auth.add_principal, w.conn, "run:w1", "worker", ["demo"]) == "validation"


def test_remint_same_run_revokes_previous(w):
    to_claimed(w, "demo:A")
    t1 = mint(w, "l1main", "demo:A", "writer", "w1")["token"]
    t2 = mint(w, "l1main", "demo:A", "writer", "w1")["token"]
    assert revoked(w, t1) and not revoked(w, t2)
    assert code(rp, w, t1) == "forbidden"   # revoked: refused at authentication


# ------------------------------------------------------------ writer use
def test_writer_token_lifecycle_and_replay(w):
    epoch, tok, p = claimed_with_token(w)
    assert p.name == "run:w1" and p.role == "worker"
    w.store.heartbeat(p, "demo:A", epoch)
    w.store.add_evidence(p, "demo:A", epoch, "decision", {"id": "D-1"})
    assert w.store.submit(p, "demo:A", epoch)["state"] == "in_review"
    assert revoked(w, tok)
    for verb in (lambda: w.store.submit(p, "demo:A", epoch),
                 lambda: w.store.heartbeat(p, "demo:A", epoch),
                 lambda: w.store.add_evidence(p, "demo:A", epoch, "decision", {})):
        assert code(verb) == "forbidden"
    actors = {r[0] for r in w.conn.execute(
        "SELECT actor FROM events WHERE type IN ('evidence_added','submitted')")}
    assert actors == {"run:w1"} and verify_integrity(w.conn) == []


def test_writer_token_limited_verbs(w):
    epoch, tok, p = claimed_with_token(w)
    s = w.store
    for fn in (lambda: s.whoami(p), lambda: s.show(p, "demo:A"),
               lambda: s.ready(p), lambda: s.snapshot(p), lambda: s.alerts(p),
               lambda: s.release(p, "demo:A", epoch),
               lambda: s.cancel(p, "demo:A", "x"),
               lambda: s.claim(p, "demo:B"), lambda: s.verdict(p, "demo:A", True),
               lambda: s.set_priority(p, "demo:A", 1),
               lambda: s.delegate(p, "demo:A", "workerA"),
               lambda: s.record_run(p, "demo:A", {"run_id": "w1", "phase": "started"}),
               lambda: s.reap(p)):
        assert code(fn) == "forbidden"
    assert not revoked(w, tok)


def test_token_for_other_item_refused(w):
    epoch, tok, p = claimed_with_token(w)
    eb = to_claimed(w, "demo:B")
    assert code(w.store.heartbeat, p, "demo:B", eb) == "forbidden"
    assert code(w.store.add_evidence, p, "demo:B", eb, "decision", {}) == "forbidden"
    assert code(w.store.submit, p, "demo:B", eb) == "forbidden"
    # a forged principal with the right key but a spoofed binding is also refused
    fake = auth.RunPrincipal("run:w1", "worker", ["demo"], None, p.token_id,
                             "demo:B", eb, "writer", "w1")
    assert code(w.store.submit, fake, "demo:B", eb) == "forbidden"


def test_wrong_role_token_refused(w):
    epoch, tok, p = claimed_with_token(w)
    forged = auth.RunPrincipal("run:w1", "reviewer", ["demo"], "teamC", p.token_id,
                               "demo:A", epoch, "reviewer", "w1")
    assert code(w.store.verdict, forged, "demo:A", True) == "forbidden"
    assert code(w.store.submit, forged, "demo:A", epoch) == "forbidden"


def test_expiry(w):
    epoch, tok, p = claimed_with_token(w)
    w.clock.advance(7201)
    assert code(w.store.heartbeat, p, "demo:A", epoch) == "forbidden"


def test_expiry_edge_and_short_lease(w):
    to_claimed(w, "demo:A", lease_s=300)
    out = mint(w, "l1main", "demo:A", "writer", "w1")
    p = rp(w, out["token"])
    w.clock.advance(299)
    w.store.heartbeat(p, "demo:A", p.epoch)        # still valid, renews
    w.clock.advance(7300)
    assert code(w.store.heartbeat, p, "demo:A", p.epoch) == "forbidden"


def test_heartbeat_renews_within_lease(w):
    to_claimed(w, "demo:A", lease_s=300)
    out = mint(w, "l1main", "demo:A", "writer", "w1")
    p = rp(w, out["token"])
    assert out["expires_at"] == w.store._item("demo:A")["lease_until"]
    w.clock.advance(200)
    w.store.heartbeat(p, "demo:A", p.epoch)
    new = w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0]
    assert new == w.store._item("demo:A")["lease_until"] > out["expires_at"]


def test_use_after_release(w):
    epoch, tok, p = claimed_with_token(w)
    w.store.release(w.p("l1main"), "demo:A", epoch)
    assert revoked(w, tok)
    assert code(w.store.heartbeat, p, "demo:A", epoch) == "forbidden"


def test_race_claim_release_reclaim(w):
    epoch, tok, p = claimed_with_token(w)
    w.store.release(w.p("l1main"), "demo:A", epoch)
    e2 = to_claimed(w, "demo:A", "l1main")
    assert e2 == epoch + 1
    for fn in (lambda: w.store.heartbeat(p, "demo:A", e2),
               lambda: w.store.heartbeat(p, "demo:A", epoch),
               lambda: w.store.submit(p, "demo:A", e2)):
        assert code(fn) == "forbidden"
    assert w.store._item("demo:A")["state"] == "claimed"


def test_unrevoked_stale_epoch_still_refused(w):
    """Defence in depth: even if a row were not revoked, the epoch check holds."""
    epoch, tok, p = claimed_with_token(w)
    w.store.release(w.p("l1main"), "demo:A", epoch)
    to_claimed(w, "demo:A", "l1main")
    w.conn.execute("UPDATE run_tokens SET revoked_at=NULL")
    assert code(w.store.heartbeat, p, "demo:A", epoch) == "forbidden"


def test_lease_expiry_reap_revokes(w):
    epoch, tok, p = claimed_with_token(w)
    w.clock.advance(7300)
    w.store.reap(w.p("system"))
    assert revoked(w, tok)
    assert code(w.store.heartbeat, p, "demo:A", epoch) == "forbidden"


def test_cancel_revokes(w):
    epoch, tok, p = claimed_with_token(w)
    w.store.cancel(w.p("router"), "demo:A", "x")
    assert revoked(w, tok)
    assert code(w.store.heartbeat, p, "demo:A", epoch) == "forbidden"


def test_close_revokes_reviewer_and_writer(w):
    to_review(w)
    tok = mint(w, "l1main", "demo:A", "reviewer", "rv1")["token"]
    w.store.verdict(w.p("reviewer"), "demo:A", True)          # done
    assert revoked(w, tok)
    assert code(rp, w, tok) == "forbidden"   # revoked: refused at authentication


def test_token_without_stored_row_in_audit(w):
    epoch, tok, p = claimed_with_token(w)
    code(w.store.release, p, "demo:A", epoch)
    msgs = [r[0] or "" for r in w.conn.execute("SELECT msg FROM audit_log")]
    assert all(tok not in m for m in msgs)
    row = w.conn.execute("SELECT actor,ok FROM audit_log WHERE action='release'"
                         " ORDER BY id DESC").fetchone()
    assert (row["actor"], row["ok"]) == ("run:w1", 0)


# ---------------------------------------------------------- reviewer use
def test_reviewer_token_verdict_and_replay(w):
    to_review(w)
    out = mint(w, "l1main", "demo:A", "reviewer", "rv1")
    p = rp(w, out["token"])
    assert (p.name, p.role, p.family) == ("run:rv1", "reviewer", "teamc")
    assert code(w.store.submit, p, "demo:A", 1) == "forbidden"
    assert code(w.store.heartbeat, p, "demo:A", 1) == "forbidden"
    assert w.store.verdict(p, "demo:A", True)["state"] == "done"
    assert revoked(w, out["token"])
    assert code(w.store.verdict, p, "demo:A", True) == "forbidden"
    assert verify_integrity(w.conn) == []


def test_reviewer_family_unknown_fails_closed(w):
    to_review(w)
    p = rp(w, mint(w, "l1main", "demo:A", "reviewer", "nofam")["token"])
    assert code(w.store.verdict, p, "demo:A", True) == "family_unknown"
    assert w.store._item("demo:A")["state"] == "in_review"


def test_reviewer_same_family_refused(w):
    RUNS["samefam"], FAMILY["samefam"] = "demo:A", "teamA"
    try:
        to_review(w)
        p = rp(w, mint(w, "l1main", "demo:A", "reviewer", "samefam")["token"])
        assert code(w.store.verdict, p, "demo:A", True) == "same_family"
    finally:
        RUNS.pop("samefam"), FAMILY.pop("samefam")


def test_reviewer_changes_requested_revokes_all(w):
    epoch = to_review(w)
    out = mint(w, "l1main", "demo:A", "reviewer", "rv1")
    other = mint(w, "l1main", "demo:A", "reviewer", "rv2")["token"]
    p = rp(w, out["token"])
    assert w.store.verdict(p, "demo:A", False, "no")["state"] == "claimed"
    assert revoked(w, out["token"]) and revoked(w, other)
    assert code(rp, w, other) == "forbidden"   # revoked: refused at authentication


def test_reviewer_token_on_other_item_or_state(w):
    to_review(w)
    p = rp(w, mint(w, "l1main", "demo:A", "reviewer", "rv1")["token"])
    to_review(w, "demo:B")
    assert code(w.store.verdict, p, "demo:B", True) == "forbidden"


def test_reviewer_token_expires(w):
    to_review(w)
    p = rp(w, mint(w, "l1main", "demo:A", "reviewer", "rv1")["token"])
    w.clock.advance(7201)
    assert code(w.store.verdict, p, "demo:A", True) == "forbidden"


def test_resubmit_after_changes_needs_new_writer_token(w):
    epoch, tok, p = claimed_with_token(w)
    w.store.add_evidence(p, "demo:A", epoch, "decision", {"id": "D-1"})
    w.store.submit(p, "demo:A", epoch)
    w.store.verdict(w.p("reviewer"), "demo:A", False, "fix")
    assert code(w.store.submit, p, "demo:A", epoch) == "forbidden"
    p2 = rp(w, mint(w, "l1main", "demo:A", "writer", "w2")["token"])
    assert w.store.submit(p2, "demo:A", epoch)["state"] == "in_review"


def test_open_item_untouched_by_old_token(w):
    """Unknown token strings never authenticate."""
    assert code(auth.authenticate, w.conn, "pgr1_nope") == "forbidden"
    assert code(auth.authenticate, w.conn, "") == "forbidden"


def test_upgrade_creates_table_on_old_db(tmp_path):
    import sqlite3
    from plangraph.keeper import db as kdb
    path = tmp_path / "old.db"
    conn = kdb.open_db(path, create=True)
    conn.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE run_tokens")
    raw.commit(); raw.close()
    conn = kdb.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM run_tokens").fetchone()[0] == 0
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("CREATE TABLE evil(x)")
    conn.close()


def test_token_never_on_stdout_stderr_or_logs(w, capfd, caplog):
    """No store operation prints or logs the plaintext token (only the CLI door prints it, by design)."""
    import logging
    caplog.set_level(logging.DEBUG)
    epoch, tok, p = claimed_with_token(w)
    w.store.heartbeat(p, "demo:A", epoch)
    w.store.add_evidence(p, "demo:A", epoch, "decision", {"id": "D-1"})
    w.store.submit(p, "demo:A", epoch)
    code(rp, w, tok)                                   # revoked: refused, message must not echo it
    out, err = capfd.readouterr()
    assert tok not in out and tok not in err
    assert tok not in caplog.text
    try:
        rp(w, tok)
    except Refused as exc:
        assert tok not in str(exc)
