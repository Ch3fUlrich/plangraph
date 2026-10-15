"""SQLite and Postgres must produce the same event chain and the same item state.

The same deterministic operation sequence (injected clock, fake verifiers, fixed
principals) is replayed on both backends; every event row, the chain head and the
cached item rows must be identical.  Needs a Postgres server: set ``PG_DSN`` (CI
provides one); skipped otherwise.  The chain-only part also runs on SQLite alone.
"""
import json
import os

import pytest

from keeper_util import World, defn, pass_review, seed, to_claimed
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (Postgres service container)")


def replay(w):
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


def dump(w):
    rows = keeper_events.read_events(w.conn)
    items = {k: dict(v) for k, v in ((i["key"], i) for i in w.store._all_items())}
    return rows, items


def test_replay_chain_verifies_on_sqlite(tmp_path):
    w = World(tmp_path, backend="sqlite")
    replay(w)
    assert keeper_events.verify_chain(w.conn) == []
    assert keeper_events.verify_integrity(w.conn) == []
    rows, _ = dump(w)
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))


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
    for left, right in zip(rows_l, rows_p):
        assert left == right, (left["seq"], left["type"])
    assert rows_l[-1]["hash"] == rows_p[-1]["hash"]          # the chain head
    assert items_l.keys() == items_p.keys()
    for key in items_l:
        assert json.dumps(items_l[key], sort_keys=True) == json.dumps(items_p[key], sort_keys=True), key
    assert keeper_events.verify_chain(pg.conn) == []
    assert keeper_events.verify_integrity(pg.conn) == []


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
