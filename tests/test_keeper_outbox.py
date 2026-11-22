"""M2B-OUTBOX: offline queue + keeper replay (dedupe, order, epoch, revocation) and the outage grace.

Mutations killed: a duplicate id executed twice, replay skipping the per-verb checks (stale epoch,
revoked token, reordered submit), a non-run principal replaying, outbox_seen editable, an outage not
extending leases/tokens (or extending without the event the token fold needs), the client writing
the token into the outbox or dropping unanswered entries.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from plangraph.keeper import outage, outbox
from plangraph.keeper.cli_verbs import Session, write
from plangraph.keeper.auth import Principal
from plangraph.keeper.db_errors import INTEGRITY
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity

from test_keeper_run_tokens import claimed_with_token, code, mint, rp, w  # noqa: F401
from test_keeper_run_tokens_doors import d, native_client, native_client_binary  # noqa: F401

SYSTEM = Principal("system", "system", [], None)


def eid(n):
    return f"{n:032x}"


def entry(n, verb, epoch, **body):
    return {"id": eid(n), "verb": verb, "body": {"key": "demo:A", "epoch": epoch, **body}}


def results(out):
    return [r["result"] for r in out["results"]]


def modern(store, principal, entries):
    return {"results": [store.write_receipt(principal, call) for call in entries]}


def events(w, kind):
    return w.conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (kind,)).fetchone()[0]


def test_replay_runs_in_order_and_dedupes(w):
    epoch, tok, p = claimed_with_token(w)
    batch = [entry(1, "heartbeat", epoch),
             entry(2, "add_evidence", epoch, kind="decision", ref={"id": "D-1"}),
             entry(3, "submit", epoch, source_round=0)]
    assert results(modern(w.store, p, batch)) == ["ok", "ok", "ok"]
    assert w.store.show(w.p("reader"), "demo:A")["state"] == "in_review"
    before = events(w, "submitted")
    again = modern(w.store, p, batch)                              # same ids again
    assert results(again) == ["duplicate"] * 3 and again["results"][2]["first"] == "ok"
    assert events(w, "submitted") == before                        # never re-run
    assert verify_integrity(w.conn) == []


def test_heartbeat_outbox_keeps_the_original_write_key(w, tmp_path, monkeypatch):
    epoch, _, principal = claimed_with_token(w)
    path = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("PG_OUTBOX", str(path))
    session = Session(url="http://127.0.0.1:1")
    monkeypatch.setattr(session, "token", lambda: "test-token")

    def lost_reply(request, *, timeout):
        body = json.loads(request.data)
        key = request.get_header("Idempotency-key")
        assert request.get_method() == "POST" and key
        w.store.retry_http_write(principal, None, key, "heartbeat", "", body,
                                 lambda: w.store.heartbeat(principal, body["key"], body["epoch"]))
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", lost_reply)
    body = {"key": "demo:A", "epoch": epoch}
    assert write(session, "heartbeat", body) == outbox.EXIT_QUEUED
    lease = w.store._item("demo:A")["lease_until"]
    w.clock.advance(60)
    assert results(w.store.replay(principal, [json.loads(path.read_text())])) == ["ok"]
    assert w.store._item("demo:A")["lease_until"] == lease


@pytest.mark.parametrize("verb,fields", [("claim", {"lease_s": 600}), ("set_priority", {"n": 7})])
@pytest.mark.parametrize("outbox_enabled", [False, True])
def test_unqueued_writes_print_the_key_sent_to_keeper(verb, fields, outbox_enabled,
                                                     tmp_path, monkeypatch, capsys):
    path = tmp_path / "outbox.jsonl"
    monkeypatch.delenv("PG_OUTBOX", raising=False)
    monkeypatch.delenv("PG_IDEMPOTENCY_KEY", raising=False)
    if outbox_enabled:
        monkeypatch.setenv("PG_OUTBOX", str(path))
    session = Session(url="http://127.0.0.1:1")
    monkeypatch.setattr(session, "token", lambda: "test-token")
    sent_keys = []

    def lost_reply(request, *, timeout):
        sent_keys.append(request.get_header("Idempotency-key"))
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", lost_reply)
    assert write(session, verb, {"key": "demo:A", **fields}) == 3
    key, = sent_keys
    assert key
    captured = capsys.readouterr()
    assert not captured.out
    assert f"outcome unknown, repeat with PG_IDEMPOTENCY_KEY={key}\n" in captured.err
    assert not path.exists()


def test_reordered_stale_and_revoked_entries_are_refused(w):
    epoch, tok, p = claimed_with_token(w)
    out = modern(w.store, p, [entry(1, "submit", epoch, source_round=0),            # before its evidence
                             entry(2, "heartbeat", epoch + 5)])    # stale epoch
    assert results(out)[0] != "ok" and results(out)[1] in ("epoch_stale", "forbidden")
    w.store.release(w.p("l1main"), "demo:A", epoch)                # revokes the token
    assert results(modern(w.store, p, [entry(3, "heartbeat", epoch)])) == ["forbidden"]


def test_only_run_tokens_replay_and_entries_are_validated(w):
    epoch, tok, p = claimed_with_token(w)
    assert code(w.store.replay, w.p("l1main"), [entry(1, "heartbeat", epoch)]) == "forbidden"
    assert code(w.store.replay, p, []) == "validation"
    assert code(w.store.replay, p, [entry(i, "heartbeat", epoch) for i in range(101)]) == "validation"
    assert code(w.store.write_receipt, p, {"id": "nope", "verb": "heartbeat", "body": {}}) == "validation"
    assert code(w.store.write_receipt, p, entry(9, "claim", epoch)) == "forbidden"
    assert code(w.store.write_receipt, w.p("l1main"), entry(9, "heartbeat", epoch)) == "forbidden"


def test_outbox_seen_is_append_only(w):
    epoch, tok, p = claimed_with_token(w)
    w.conn.execute("INSERT INTO outbox_seen VALUES(?,?,?,?,?,?)",
                   (eid(1), p.token_id, "heartbeat", "demo:A", "ok", w.store._iso(w.store.now())))
    w.store.write_receipt(p, entry(2, "heartbeat", epoch))
    for table in ("outbox_seen", "op_receipts"):
        for sql in (f"UPDATE {table} SET result='x'", f"DELETE FROM {table}"):
            with pytest.raises((sqlite3.DatabaseError, *INTEGRITY)):
                w.conn.execute(sql)


def test_outage_extends_leases_and_tokens_and_verify_follows(w):
    epoch, tok, p = claimed_with_token(w)
    lease0 = w.store.show(w.p("reader"), "demo:A")["lease_until"]
    exp0 = w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0]
    now = w.store.now()
    out = w.store.record_outage(SYSTEM, 3600, now - 10, now)
    assert out["leases"] == 1 and out["tokens"] == 1 and events(w, "outage_recorded") == 1
    assert w.store.show(w.p("reader"), "demo:A")["lease_until"] > lease0
    assert w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0] > exp0
    assert verify_integrity(w.conn) == []                          # fold replays the grace
    w.clock.advance(7200 + 60)                                     # past the original expiry
    w.store.heartbeat(p, "demo:A", epoch)                          # honoured for the outage
    assert code(w.store.record_outage, w.p("router"), 10, now, now) == "forbidden"


def test_outage_grace_is_capped_per_token_across_restarts(w):
    """Repeated outages cannot keep one token alive: total grace per token <= 24 h (seat V7)."""
    epoch, tok, p = claimed_with_token(w)
    exp0 = w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0]
    now = w.store.now()
    assert w.store.record_outage(SYSTEM, 50000, now - 10, now)["tokens"] == 1
    exp1 = w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0]
    assert exp1 > exp0
    out = w.store.record_outage(SYSTEM, 50000, now + 5, now + 10)   # 100000 s total > cap
    assert out["tokens"] == 0 and out["leases"] == 1                 # leases still follow
    assert w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0] == exp1
    assert verify_integrity(w.conn) == []


def test_verify_flags_grace_over_the_cap(w):
    from plangraph.keeper import token_fold
    epoch, tok, p = claimed_with_token(w)
    now = w.store.now()
    w.store.record_outage(SYSTEM, 3600, now - 10, now)
    assert verify_integrity(w.conn) == []
    old = token_fold.MAX_GRACE_S
    token_fold.MAX_GRACE_S = 60
    try:
        assert any(pr.get("field") == "grace_s" for pr in verify_integrity(w.conn))
    finally:
        token_fold.MAX_GRACE_S = old


def test_check_and_record_uses_the_alive_stamp(w, tmp_path):
    cfg = SimpleNamespace(db=str(tmp_path / "keeper.db"), reap_interval=60)
    assert outage.check_and_record(w.store, SYSTEM, cfg) is None   # first start: stamp only
    assert outage.alive_path(cfg).exists()
    now = w.store.now()
    os.utime(outage.alive_path(cfg), (now - 30, now - 30))
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now) is None   # short gap
    os.utime(outage.alive_path(cfg), (now - 900, now - 900))
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now)["gap_s"] == 900


def test_unproven_legacy_entries_remain_unacknowledged(w):
    epoch, _, p = claimed_with_token(w)
    call = entry(7, "heartbeat", epoch)
    assert w.store.replay(p, [call]) == {
        "results": [], "recovery": [{"id": call["id"], "result": "recovery_required"}]}
    assert w.store.write_receipt(p, {**call, "legacy": True})["result"] == "recovery_required"
    assert w.conn.execute("SELECT COUNT(*) FROM op_receipts").fetchone()[0] == 0


def _outbox_writer(doors, monkeypatch):
    epoch, token, principal = claimed_with_token(doors.world)
    doors.world.tokens["runw"] = token
    monkeypatch.setenv("PG_EPOCH", str(epoch))
    return epoch, token, principal


def test_client_queues_without_the_token_and_replay_keeps_unanswered(native_client, monkeypatch):
    doors = native_client
    epoch, token, _ = _outbox_writer(doors, monkeypatch)
    store = doors.world.store
    store.add_evidence(doors.world.p("l1main"), "demo:A", epoch, "decision", {"id": "D-1"})
    write = store.write_receipt
    allowed = set()

    def intermittent(principal, call):
        if call["id"] not in allowed:
            raise Refused("unavailable", "test outage")
        return write(principal, call)

    monkeypatch.setattr(store, "write_receipt", intermittent)
    assert doors.pg("runw", "heartbeat", "demo:A")[0] == 4
    assert doors.pg("runw", "submit", "demo:A")[0] == 4
    assert doors.pg("runw", "claim", "demo:A")[0] == 2
    queue = doors.tmp / "outbox.jsonl.d"
    first, second = sorted(queue.glob("*.json"))
    first_id = json.loads(first.read_text())["id"]
    second_id = json.loads(second.read_text())["id"]
    assert first_id != second_id
    assert all(token not in path.read_text() for path in (first, second, queue / "credential"))
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (first, second))

    allowed.add(first_id)
    assert doors.pg("runw", "replay")[0] == 4
    assert not first.exists()
    assert json.loads((queue / "done" / first.name).read_text())["id"] == first_id
    assert json.loads(second.read_text())["id"] == second_id

    allowed.add(second_id)
    assert doors.pg("runw", "replay")[0] == 0
    assert not list(queue.glob("*.json"))
    assert store.conn.execute("SELECT COUNT(*) FROM op_receipts").fetchone()[0] == 2
    assert events(doors.world, "submitted") == 1


def test_client_drops_non_object_lines(native_client, monkeypatch):
    doors = native_client
    epoch, _, _ = _outbox_writer(doors, monkeypatch)
    legacy = doors.tmp / "outbox.jsonl"
    original = '"not an entry"\n[1, 2]\n' + json.dumps(entry(7, "heartbeat", epoch)) + "\n"
    legacy.write_text(original)
    calls = []

    def record_call(principal, call):
        calls.append(call)
        return {"id": call["id"], "result": "recovery_required"}

    monkeypatch.setattr(doors.world.store, "write_receipt", record_call)
    status, _, error = doors.pg("runw", "replay")
    assert status == 4 and "requires recovery" in error
    assert calls == []
    assert legacy.read_text() == original
    queue = doors.tmp / "outbox.jsonl.d"
    *malformed, valid = [json.loads(path.read_text()) for path in sorted(queue.glob("*.json"))]
    assert len(malformed) == 2
    assert all(row["verb"] == "unreadable" and row["body"] is None for row in malformed)
    assert valid["id"] == eid(7) and valid["legacy"] is True


def test_client_refuses_symlinked_outbox(native_client, monkeypatch):
    doors = native_client
    _outbox_writer(doors, monkeypatch)
    target = doors.tmp / "target"
    target.write_text("unchanged")
    legacy = doors.tmp / "outbox.jsonl"
    legacy.symlink_to(target)
    status, _, error = doors.pg("runw", "heartbeat", "demo:A")
    assert status != 0 and "target must be a regular file" in error
    assert target.read_text() == "unchanged"
    assert not (doors.tmp / "outbox.jsonl.d").exists()
    assert doors.world.conn.execute("SELECT COUNT(*) FROM op_receipts").fetchone()[0] == 0


def test_outage_gap_is_capped_and_untrusted_stamps_are_ignored(w, tmp_path):
    cfg = SimpleNamespace(db=str(tmp_path / "keeper.db"), reap_interval=60)
    outage.stamp(cfg)
    path = outage.alive_path(cfg)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    now = w.store.now()
    os.utime(path, (0, 0))                                         # an ancient / forged stamp
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now)["gap_s"] == outage.OUTAGE_MAX_S
    os.utime(path, (now - 900, now - 900))
    os.chmod(path, 0o666)                                          # writable by others: ignored
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now) is None
    os.utime(path, (now + 3600, now + 3600))                       # from the future: ignored
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now) is None
    assert w.store.record_outage(SYSTEM, 10 ** 9, now, now)["gap_s"] == 86400


def test_malformed_replay_bodies_are_validation_not_500(w):
    epoch, tok, p = claimed_with_token(w)
    out = w.store.write_receipt(p, {"id": eid(1), "verb": "add_evidence",
                                    "body": {"key": "demo:A", "epoch": "e", "kind": 123, "ref": "x"}})
    assert out["result"] in ("validation", "forbidden", "epoch_stale")
    assert code(w.store.write_receipt, p, {
        "id": eid(2), "verb": "heartbeat", "body": "not a dict"}) == "validation"


def test_outage_is_applied_once_per_start(w):
    claimed_with_token(w)
    now = w.store.now()
    exp = lambda: w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0]
    w.store.record_outage(SYSTEM, 600, now - 700, now)
    once = exp()
    assert w.store.record_outage(SYSTEM, 600, now - 700, now)["duplicate"] is True
    assert exp() == once and events(w, "outage_recorded") == 1
    assert verify_integrity(w.conn) == []


def test_per_token_replay_limit(w, monkeypatch):
    from plangraph.keeper import store_outbox
    epoch, _, p = claimed_with_token(w)
    monkeypatch.setattr(store_outbox, "MAX_SEEN_PER_TOKEN", 3)
    calls = [entry(i, "heartbeat", epoch) for i in range(3)]
    assert results(modern(w.store, p, calls)) == ["ok"] * 3
    assert code(w.store.write_receipt, p, entry(9, "heartbeat", epoch)) == "conflict"
    assert results(modern(w.store, p, calls)) == ["duplicate"] * 3


def test_foreign_owner_stamp_is_ignored(w, tmp_path, monkeypatch):
    cfg = SimpleNamespace(db=str(tmp_path / "keeper.db"), reap_interval=60)
    outage.stamp(cfg)
    now = w.store.now()
    os.utime(outage.alive_path(cfg), (now - 900, now - 900))
    other = os.getuid() + 1
    monkeypatch.setattr(outage.os, "getuid", lambda: other)        # the stamp is "not ours"
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now) is None


def test_extension_without_the_outage_event_is_flagged(w):
    from test_keeper_run_tokens import to_review
    to_review(w)
    mint(w, "l1main", "demo:A", "reviewer", "rv1")
    expires = w.conn.execute("SELECT expires_at FROM run_tokens WHERE role='reviewer'").fetchone()[0]
    later = w.store._shift(expires, 600)
    w.conn.execute("UPDATE run_tokens SET expires_at=? WHERE role='reviewer'", (later,))
    assert any(p["field"] == "expires_at" for p in verify_integrity(w.conn))   # no grace without the event


def test_concurrent_http_replays_of_one_id_run_once(d):
    import threading

    epoch, d.world.tokens["runw"], _ = claimed_with_token(d.world)
    payload = entry(7, "heartbeat", epoch)
    got = []
    threads = [threading.Thread(target=lambda: got.append(
        d.http("runw", "POST", "/v1/write_receipt", payload)[1]["result"]))
        for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(got) == ["duplicate", "duplicate", "duplicate", "ok"]


def test_model_vectors_run_against_keeper(w):
    from pathlib import Path

    epoch, _, p = claimed_with_token(w)
    w.store.add_evidence(p, "demo:A", epoch, "decision", {"id": "D-1"})
    vectors = Path(__file__).parents[1] / "rust/pg-receipts/tests/keeper-adoption.tsv"
    for line in vectors.read_text().splitlines():
        verb, invocation, source, current, expected = line.split("\t")
        w.conn.execute("UPDATE items SET round=? WHERE key='demo:A'", (int(current),))
        call = entry(int(invocation), verb, epoch)
        if verb == "submit":
            call["body"]["source_round"] = int(source)
        assert w.store.write_receipt(p, call)["result"] == expected
    assert events(w, "submitted") == 1


def test_http_receipt_readback_after_token_revocation(d):
    epoch, d.world.tokens["runw"], _ = claimed_with_token(d.world)
    add = entry(1, "add_evidence", epoch, kind="decision", ref={"id": "D-1"})
    path = "/v1/write_receipt"
    assert d.http("runw", "POST", path, add)[1]["result"] == "ok"
    call = entry(2, "submit", epoch, source_round=0)
    status, committed = d.http("runw", "POST", path, call)
    assert status == 200 and committed["result"] == "ok"
    status, readback = d.http("runw", "POST", path, call)
    assert status == 200 and readback["result"] == "duplicate"
    assert readback["reply"] == committed["reply"]
    assert d.http("reader", "POST", path, call)[0] == 403
    assert d.http("runw", "POST", path, {**call, "id": eid(3)})[0] == 401
    assert events(d.world, "submitted") == 1


def test_crash_recording_receipt_rolls_back_mutation(w, monkeypatch):
    epoch, _, p = claimed_with_token(w)
    call = entry(1, "add_evidence", epoch, kind="decision", ref={"id": "D-1"})
    insert = w.store._insert_receipt

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted before receipt insert")

    monkeypatch.setattr(w.store, "_insert_receipt", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        w.store.write_receipt(p, call)
    assert events(w, "evidence_added") == 0
    monkeypatch.setattr(w.store, "_insert_receipt", insert)
    assert w.store.write_receipt(p, call)["result"] == "ok"
    assert events(w, "evidence_added") == 1




def test_legacy_receipt_returns_historical_outcome_only(w):
    epoch, _, p = claimed_with_token(w)
    _, _, other = claimed_with_token(w, run="other", key="demo:B")
    call = entry(1, "heartbeat", epoch)
    w.conn.execute("INSERT INTO outbox_seen VALUES(?,?,?,?,?,?)",
                   (call["id"], p.token_id, "heartbeat", None, "ok", w.store._iso(w.store.now())))
    assert w.store.write_receipt(p, {**call, "legacy": True}) == {
        "id": call["id"], "result": "duplicate", "first": "ok", "legacy": True}
    assert code(w.store.write_receipt, other, call) == "forbidden"
    assert w.conn.execute("SELECT COUNT(*) FROM op_receipts").fetchone()[0] == 0


def test_transient_refusal_does_not_become_a_receipt(w, monkeypatch):
    epoch, _, p = claimed_with_token(w)
    call = entry(1, "heartbeat", epoch)
    heartbeat = w.store.heartbeat

    def busy(*args, **kwargs):
        raise Refused("conflict", "keeper database is busy")

    monkeypatch.setattr(w.store, "heartbeat", busy)
    assert code(w.store.write_receipt, p, call) == "unavailable"
    assert w.conn.execute("SELECT COUNT(*) FROM op_receipts").fetchone()[0] == 0
    monkeypatch.setattr(w.store, "heartbeat", heartbeat)
    assert w.store.write_receipt(p, call)["result"] == "ok"


def test_sqlite_legacy_receipts_are_imported_on_upgrade(tmp_path):
    from plangraph.keeper import db as kdb, schema

    path = tmp_path / "v17.db"
    with sqlite3.connect(path) as raw:
        from plangraph.keeper.schema_guards import RECEIPTS_DDL
        raw.executescript(schema.SCHEMA.replace(RECEIPTS_DDL, ""))
        raw.execute("INSERT INTO outbox_seen VALUES (?, 7, 'heartbeat', NULL, 'ok', 'then')",
                    ("01" * 16,))
    for _ in range(2):
        conn = kdb.open_db(path, backend="sqlite")
        try:
            rows = conn.execute("SELECT token_id,request_hash,result FROM op_receipts").fetchall()
            assert [tuple(row) for row in rows] == [(7, None, "ok")]
        finally:
            conn.close()


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not os.environ.get("PG_DSN"), reason="PG_DSN not set"))])
def test_mixed_version_legacy_and_fresh_receipt_replay(tmp_path, backend):
    from keeper_util import World, seed
    from plangraph.keeper import db as kdb
    from plangraph.keeper.store import Store
    from test_keeper_run_tokens import FakeRuns

    pg_dsn = os.environ.get("PG_DSN")
    world = World(tmp_path, backend=backend, dsn=pg_dsn)
    seed(world, [("A", [])])
    world.store.ctx = {"runs": FakeRuns()}
    epoch, _, principal = claimed_with_token(world)
    old = {"id": "01" * 16, "verb": "heartbeat", "body": {"key": "demo:A", "epoch": epoch}}
    world.conn.execute("INSERT INTO outbox_seen VALUES(?,?,?,?,?,?)",
                       (old["id"], principal.token_id, "heartbeat", None, "ok", "then"))
    pg_schema = getattr(world.conn, "schema", None)
    world.close()
    if pg_schema is None:
        with sqlite3.connect(world.path) as raw:
            raw.execute("DROP TABLE op_receipts")
    else:
        import psycopg
        from psycopg import sql

        with psycopg.connect(pg_dsn, autocommit=True) as raw:
            raw.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(pg_schema)))
            raw.execute("DROP TABLE op_receipts")
            raw.execute("DELETE FROM schema_migrations WHERE version=15")

    conn = kdb.open_db(world.path, backend=backend, dsn=pg_dsn, schema=pg_schema,
                       create=pg_schema is not None)
    try:
        store = Store(conn, clock=world.clock)
        known = {"id": old["id"], "result": "duplicate", "first": "ok", "legacy": True}
        assert conn.execute("SELECT COUNT(*) FROM op_receipts").fetchone()[0] == 1
        assert store.replay(principal, [old]) == {"results": [known], "recovery": []}
        assert store.write_receipt(principal, {**old, "legacy": True}) == known
        fresh = dict(old, id="02" * 16)
        assert store.write_receipt(principal, fresh)["result"] == "ok"
        assert store.replay(principal, [fresh])["results"][0]["first"] == "ok"
    finally:
        conn.close()
