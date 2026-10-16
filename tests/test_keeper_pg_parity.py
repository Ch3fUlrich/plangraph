"""SQLite and Postgres must produce the same event chain and the same item state.

The same deterministic operation sequence (injected clock, fake verifiers, fixed
principals) is replayed on both backends; every event row, the chain head and the
cached item rows must be identical.  The workload includes the design-3 verbs (``state_set`` events and the cards,
attempts, decisions, questions, answers and budget rows).  Needs a Postgres server: set
``PG_DSN`` (CI provides one); skipped otherwise.  The chain-only part also runs on SQLite alone.
"""
import json
import os

import pytest

from keeper_util import World, defn, pass_review, seed, to_claimed
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (Postgres service container)")


def replay_legacy(w):
    """A mixed workload: create, claim, evidence, review, edit, cancel, refusals, a rollback."""
    router, lead = w.p("router"), w.p("l1main")
    keys = seed(w, [("A", []), ("B", ["A"]), ("C", []), ("D", [])])
    a, b, c, d = keys
    epoch = to_claimed(w, a)
    w.store.add_evidence(lead, a, epoch, "decision", {"id": "D-1"})
    w.store.heartbeat(lead, a, epoch)
    w.store.submit(lead, a, epoch)
    pass_review(w, a)
    w.store.set_priority(router, c, 3)
    w.store.edit_definition(router, c, {"title": "renamed c é中", "estimate_h": 2.5})
    w.clock.advance(30)
    to_claimed(w, b, who="workerA")
    w.store.cancel(router, d, "dropped")
    with pytest.raises(Refused):                      # refused write: audited, no event
        w.store.claim(w.p("reader"), c)
    with pytest.raises(Refused):                      # locked (done) item
        w.store.set_priority(router, a, 9)
    return keys


def replay_w2(w, keys):
    """The design-3 verbs: state_set events plus plain rows in the new tables."""
    router, lead, system = w.p("router"), w.p("l1main"), w.p("system")
    a, b, c, d = keys
    w.clock.advance(60)
    w.store.set_state(router, c, "ready", "readiness gate passed")
    w.store.set_state(lead, c, "running")
    w.store.put_card(router, c, {
        "outcome": "é中 outcome", "write_set": ["src/x.py"], "read_set": ["src/y.py"],
        "check_cmd": "pytest -q", "window_class": "256k", "risk_tags": ["auth"],
        "estimate_tokens": 12345})
    w.store.record_attempt(system, c, {
        "harness": "h", "model": "m", "combo": "h/m", "class": "256k", "n": 1,
        "started_at": "2026-10-15T10:00:00Z", "ended_at": "2026-10-15T10:01:00Z",
        "exit_class": "needs_input", "tokens_in": 10, "tokens_out": 5, "cost": 0.125,
        "run_id": "r1", "workflow_id": "wf1", "log_ref": "l1"})
    w.store.set_state(system, c, "needs_input", "question asked")
    qid = w.store.ask_question(system, c, "which one?", risk_tags=["money"])["id"]
    quick = w.store.ask_question(lead, c, "colour?", default_answer="blue",
                                 deadline="2023-11-15T00:00:00Z")["id"]
    w.store.add_decision(router, "D-900", "operator", "pick the first", closes=[c])
    w.store.add_decision(router, "D-901", "judge", "ruling", waives=[a])
    w.store.answer_question(router, qid, "the first")
    w.clock.advance(86400)
    w.store.answer_question(system, quick, source="question_timeout")
    w.store.set_state(system, c, "waiting", "timer")
    w.store.set_budget(router, "claude", "2026-10-15", 1500, 2.5, meter=0.4)
    w.store.set_budget(router, "claude", "2026-10-15", 1800, 3.0, meter=0.5)
    w.store.set_metrics(router, "2026-10-15", [{"metric": "claude_weighted_tokens",
                                                "value": 1800.5, "source": "transcripts"}])
    w.store.set_metrics(router, "2026-10-15", [{"metric": "claude_weighted_tokens",
                                                "value": 2000.5, "source": "transcripts"}])
    w.store.set_state(lead, c, "failed")
    w.store.set_state(lead, c, "parked")
    w.store.set_state(lead, c, "open")
    with pytest.raises(Refused):                      # claim states keep their own verbs
        w.store.set_state(router, c, "claimed")


def replay(w):
    keys = replay_legacy(w)
    replay_w2(w, keys)
    return keys


_TABLES = {
    "cards": "key", "attempts": "id", "decisions": "id", "questions": "id",
    "answers": "question_id", "budget": "provider, day", "metrics": "day, metric",
}


def dump(w):
    rows = keeper_events.read_events(w.conn)
    items = {k: dict(v) for k, v in ((i["key"], i) for i in w.store._all_items())}
    return rows, items


def dump_tables(w):
    return {name: [dict(r) for r in w.conn.execute(f"SELECT * FROM {name} ORDER BY {order}")]
            for name, order in _TABLES.items()}


#: head of ``replay_legacy`` as produced before the design-3 work (schema v9 code): the hash
#: chain of every pre-existing event kind must stay byte-identical.
#:
#: ``PG-ESTIMATE-SRC-EDIT`` widened the estimate edit's ``item_defined`` payload
#: (seq 12 now carries the ``estimate_h``/``estimate_src`` values and the
#: auto-filled source), so the chain legitimately moves from that event on --
#: every hash after it is only the old chain seen through one wider payload.
#: ``LEGACY_PREFIX`` pins the last head *before* it, which all the other event
#: kinds still hash to byte for byte.
LEGACY_PREFIX = (11, "58618aa115472f589e7ee07531c557e36f5257f926792887f16401aa3458185d")
LEGACY_HEAD = (14, "16badb7cba6b9e76f147a98c0a1aea17eefbfbd12f27eb0e71d6480b2d08845f")


def test_replay_chain_verifies_on_sqlite(tmp_path):
    w = World(tmp_path, backend="sqlite")
    replay(w)
    assert keeper_events.verify_chain(w.conn) == []
    assert keeper_events.verify_integrity(w.conn) == []
    rows, _ = dump(w)
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert "state_set" in {r["type"] for r in rows}
    assert rows[LEGACY_PREFIX[0] - 1]["hash"] == LEGACY_PREFIX[1]   # prefix unchanged
    assert rows[LEGACY_HEAD[0] - 1]["hash"] == LEGACY_HEAD[1]       # legacy prefix unchanged


def test_existing_event_kinds_hash_exactly_as_before(tmp_path):
    w = World(tmp_path, backend="sqlite")
    replay_legacy(w)
    rows, _ = dump(w)
    assert rows[LEGACY_PREFIX[0] - 1]["hash"] == LEGACY_PREFIX[1]
    assert (len(rows), rows[-1]["hash"]) == LEGACY_HEAD
    assert "state_set" not in {r["type"] for r in rows}


@needs_pg
def test_same_ops_give_identical_events_and_items(tmp_path):
    lite_dir = tmp_path / "lite"
    lite_dir.mkdir()
    lite = World(lite_dir, backend="sqlite")
    pg_dir = tmp_path / "pg"
    pg_dir.mkdir()
    pg = World(pg_dir, backend="postgres", dsn=DSN)
    replay(lite)
    replay(pg)
    rows_l, items_l = dump(lite)
    rows_p, items_p = dump(pg)
    assert len(rows_l) == len(rows_p) > 10
    assert "state_set" in {r["type"] for r in rows_l}
    assert [(r["seq"], r["hash"]) for r in rows_l] == [(r["seq"], r["hash"]) for r in rows_p]
    for left, right in zip(rows_l, rows_p):
        assert left == right, (left["seq"], left["type"])
    assert rows_l[-1]["hash"] == rows_p[-1]["hash"]          # the chain head
    assert rows_p[LEGACY_HEAD[0] - 1]["hash"] == LEGACY_HEAD[1]
    tables_l, tables_p = dump_tables(lite), dump_tables(pg)
    assert tables_l["cards"] and tables_l["attempts"] and tables_l["answers"]
    assert json.dumps(tables_l, sort_keys=True) == json.dumps(tables_p, sort_keys=True)
    assert items_l.keys() == items_p.keys()
    for key in items_l:
        assert json.dumps(items_l[key], sort_keys=True) == json.dumps(items_p[key], sort_keys=True), key
    assert keeper_events.verify_chain(pg.conn) == []
    assert keeper_events.verify_integrity(pg.conn) == []


@needs_pg
def test_design3_triggers_refuse_tampering(tmp_path):
    from plangraph.keeper import db_errors
    d = tmp_path / "tamper3"
    d.mkdir()
    w = World(d, backend="postgres", dsn=DSN)
    replay(w)
    for sql in ("UPDATE attempts SET exit_class='ok'", "DELETE FROM attempts",
                "UPDATE decisions SET text='x'", "DELETE FROM decisions",
                "UPDATE answers SET text='x'", "DELETE FROM answers"):
        with pytest.raises(db_errors.DENIED + db_errors.INTEGRITY):
            w.conn.execute(sql)
    with pytest.raises(db_errors.INTEGRITY):
        w.conn.execute("INSERT INTO attempts(id,key,n,exit_class) VALUES(99,'demo:C',9,'bogus')")
    with pytest.raises(db_errors.INTEGRITY):
        w.conn.execute("INSERT INTO cards(key,outcome,window_class) VALUES('demo:A','o','2m')")


@needs_pg
def test_text_ordering_matches_sqlite(tmp_path):
    names = ["demo:B", "demo:a", "demo:A-1", "demo:_x", "demo:Z", "demo:b-2", "demo:é"]
    out = []
    for backend, dsn in (("sqlite", None), ("postgres", DSN)):
        d = tmp_path / backend
        d.mkdir()
        w = World(d, backend=backend, dsn=dsn)
        w.store.create_items(w.p("router"), [
            defn(n.split(":", 1)[1], [], kind="goal") for n in names])
        out.append([i["key"] for i in w.store._all_items()])
    assert out[0] == out[1]


@needs_pg
def test_write_lock_serialises_two_connections(tmp_path):
    """Two connections to one keeper schema: exactly one claim wins (advisory write lock)."""
    import threading
    from plangraph.keeper import db as kdb
    from plangraph.keeper.store import Store
    d = tmp_path / "race"
    d.mkdir()
    w = World(d, backend="postgres", dsn=DSN)
    (key,) = seed(w, [("A", [])])
    conn2 = kdb.open_db(w.path, backend="postgres", dsn=DSN)
    other = Store(conn2, clock=w.clock, verifiers=w.verifiers)
    results = []
    gate = threading.Barrier(2)

    def go(store, who):
        gate.wait()
        try:
            store.claim(w.p(who), key)
            results.append("won")
        except Refused as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go, args=(w.store, "workerA")),
               threading.Thread(target=go, args=(other, "workerB"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(results) == ["conflict", "won"] or sorted(results) == ["bad_state", "won"], results
    conn2.close()


@needs_pg
def test_triggers_refuse_tampering(tmp_path):
    from plangraph.keeper import db_errors
    d = tmp_path / "tamper"
    d.mkdir()
    w = World(d, backend="postgres", dsn=DSN)
    replay(w)
    with pytest.raises(db_errors.DENIED + db_errors.INTEGRITY):
        w.conn.execute("UPDATE events SET payload='{}' WHERE seq=1")
    with pytest.raises(db_errors.DENIED + db_errors.INTEGRITY):
        w.conn.execute("DELETE FROM audit_log")
    with pytest.raises(db_errors.DENIED):
        w.conn.execute("DROP TRIGGER trg_events_append_only_update ON events")
    with pytest.raises(db_errors.DENIED):
        w.conn.execute("SELECT 1; DROP TABLE events")
