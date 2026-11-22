"""Hash-chain, fold and integrity tests."""
import sqlite3

from keeper_util import World, seed, to_done


def tamper(path, seq, column, value):
    """Rewrite one event through a raw connection, as an attacker would."""
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER trg_events_append_only_update")
    conn.execute(f"UPDATE events SET {column}=? WHERE seq=?", (value, seq))
    conn.commit()
    conn.close()


def test_genesis_prev_hash_is_empty(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    prev = world.conn.execute(
        "SELECT prev_hash FROM events ORDER BY seq LIMIT 1").fetchone()[0]
    assert prev == ""
    world.close()


def test_clean_chain_and_integrity(tmp_path):
    world = World(tmp_path)
    (key,) = seed(world, [("A", [])])
    to_done(world, key)
    assert world.store.verify_chain() == []
    assert world.store.verify_integrity() == []
    world.close()


def test_tampered_payload_detected(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    seq = world.conn.execute("SELECT min(seq) FROM events").fetchone()[0]
    tamper(world.path, seq, "payload", '{"evil": true}')
    problems = world.store.verify_chain()
    assert any(p["code"] == "hash_mismatch" for p in problems)
    world.close()


def test_broken_prev_hash_detected(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    seq = world.conn.execute("SELECT max(seq) FROM events").fetchone()[0]
    tamper(world.path, seq, "prev_hash", "deadbeef")
    problems = world.store.verify_chain()
    assert any(p["code"] == "prev_hash_mismatch" for p in problems)
    world.close()


def test_cache_tamper_detected_by_integrity(tmp_path):
    world = World(tmp_path)
    (key,) = seed(world, [("A", [])])
    raw = sqlite3.connect(world.path)
    raw.execute("UPDATE items SET state='claimed' WHERE key=?", (key,))
    raw.commit()
    raw.close()
    problems = world.store.verify_integrity()
    assert any(p["code"] == "cache_mismatch" and p["key"] == key
               for p in problems)
    world.close()


def test_missing_cache_row_detected(tmp_path):
    world = World(tmp_path)
    (key,) = seed(world, [("A", [])])
    # B is created by the graph; delete it through a raw connection (open, so
    # the lock trigger does not stand in the way).
    raw = sqlite3.connect(world.path)
    raw.execute("DELETE FROM items WHERE key='demo:GOAL'")
    raw.commit()
    raw.close()
    problems = world.store.verify_integrity()
    assert any(p["code"] == "cache_missing" and p["key"] == "demo:GOAL"
               for p in problems)
    world.close()


def test_releasing_review_pass_reaches_claimant_and_delegate(tmp_path):
    world = World(tmp_path)
    try:
        (key,) = seed(world, [("A", [])])
        store = world.store
        claimant = world.p("workerA")
        delegate = world.p("workerB")
        epoch = store.claim(claimant, key)["epoch"]
        store.delegate(claimant, key, delegate)
        store.add_evidence(claimant, key, epoch, "decision", {"id": "D-1"})
        store.submit(claimant, key, epoch)
        cursor = world.conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]

        with store._write():
            store._pass(world.p("reviewer"), key, "merge close", store._item(key),
                        world.clock(), reviews_passed=1, release=True)

        item = store._item(key)
        assert (item["state"], item["claimed_by"], item["delegate"]) == ("done", None, None)
        for principal in (claimant, delegate):
            rows = store.event_page(principal, cursor, 100)["rows"]
            assert [(row["key"], row["type"], row["payload"]["released"])
                    for row in rows] == [(key, "review_passed", True)]
        assert store.event_page(world.p("workerC"), cursor, 100)["rows"] == []
        assert store.verify_integrity() == []
    finally:
        world.close()
