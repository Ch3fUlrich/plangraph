"""PG-MODELS: spawner ``record_run`` verb, cached columns and derived reads."""
import json
import sqlite3
import threading
import urllib.request

import pytest

from keeper_util import World, defn, seed, to_claimed
from plangraph.keeper import api, auth, config, fmt, mcp
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    token = auth.add_principal(world.conn, "spawner1", "spawner", [])
    world.principals["spawner1"] = auth.authenticate(world.conn, token)
    world.tokens["spawner1"] = token
    yield world
    world.close()


def sp(w):
    return w.p("spawner1")


def started(rid="r1", **kw):
    run = {"run_id": rid, "phase": "started"}
    run.update(kw)
    return run


def test_happy_path_started_finished(w):
    to_claimed(w, "demo:A")
    out = w.store.record_run(sp(w), "demo:A", started(
        writer={"model": "m1", "family": "f1", "source": "s"},
        seats=[{"model": "s1", "verdict": "pass", "run_id": "sr1"}]))
    assert out["writer_model"] == "m1"
    assert out["seat_models"] == [{"model": "s1", "verdict": "pass"}]
    assert out["run_ids"] == ["r1", "sr1"]
    out2 = w.store.record_run(sp(w), "demo:A",
                              {"run_id": "r1", "phase": "finished", "rc": 0,
                               "writer": {"model": "m2"},
                               "seats": [{"model": "s2", "verdict": "fail"}]})
    assert out2["writer_model"] == "m2"
    assert out2["seat_models"] == [{"model": "s2", "verdict": "fail"}]
    assert out2["run_ids"] == ["r1", "sr1"]
    assert verify_integrity(w.conn) == []


def test_show_snapshot_and_text(w):
    to_claimed(w, "demo:A")
    w.store.record_run(sp(w), "demo:A", started(
        writer={"model": "m1"}, seats=[{"model": "s1", "verdict": "pass"}]))
    info = w.store.show(w.p("reader"), "demo:A")
    assert info["writer_model"] == "m1"
    assert info["seat_models"] == [{"model": "s1", "verdict": "pass"}]
    assert info["run_ids"] == ["r1"]
    assert info["created_at"] and info["done_at"] is None
    assert info["reviewed"] is False and info["merged"] is False
    assert "models writer=m1 seats=s1:pass" in fmt.show_text(info)
    row = next(i for i in w.store.snapshot(w.p("reader"))["items"]
               if i["key"] == "demo:A")
    assert row["writer_model"] == "m1"
    assert row["seat_models"] == [{"model": "s1", "verdict": "pass"}]
    assert row["run_ids"] == ["r1"]
    assert row["created_at"] and row["done_at"] is None
    assert row["reviewed"] is False and row["merged"] is False
    # no models recorded: text has no models line, snapshot defaults
    info2 = w.store.show(w.p("reader"), "demo:GOAL")
    assert info2["writer_model"] is None and info2["seat_models"] is None
    assert info2["run_ids"] == []
    assert "models " not in fmt.show_text(info2)


def test_duplicate_is_idempotent(w):
    to_claimed(w, "demo:A")
    first = w.store.record_run(sp(w), "demo:A", started())
    dup = w.store.record_run(sp(w), "demo:A", started())
    assert dup == {"key": "demo:A", "run_id": "r1", "phase": "started",
                   "duplicate": True}
    n = w.conn.execute(
        "SELECT COUNT(*) FROM events WHERE key=? AND type='run_recorded'",
        ("demo:A",)).fetchone()[0]
    assert n == 1
    assert first["run_ids"] == ["r1"]
    assert verify_integrity(w.conn) == []


def test_non_spawner_forbidden(w):
    to_claimed(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.record_run(w.p("workerA"), "demo:A", started())
    assert exc.value.code == "forbidden"


def test_open_item_bad_state(w):
    with pytest.raises(Refused) as exc:
        w.store.record_run(sp(w), "demo:A", started())
    assert exc.value.code == "bad_state"


def test_done_item_locked(w):
    from keeper_util import pass_review
    to_claimed(w, "demo:A")
    w.store.record_run(sp(w), "demo:A", started())
    epoch = w.store._item("demo:A")["epoch"]
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    pass_review(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.record_run(sp(w), "demo:A", started("r2"))
    assert exc.value.code == "locked"


@pytest.mark.parametrize("run", [
    {"run_id": "bad id!", "phase": "started"},
    {"run_id": "r1", "phase": "bogus"},
    {"run_id": "r1", "phase": "started", "nope": 1},
    {"run_id": "r1", "phase": "started",
     "seats": [{"model": "s", "verdict": "maybe"}]},
    {"run_id": "r1", "phase": "started", "rc": "zero"},
    {"run_id": "r1", "phase": "started", "item_key": 7},
    {"run_id": "r1", "phase": "started", "item_key": ["demo:A"]},
    {"phase": "started"},
    {"run_id": "r1"},
])
def test_bad_runs_are_validation(w, run):
    to_claimed(w, "demo:A")
    with pytest.raises(Refused) as exc:
        w.store.record_run(sp(w), "demo:A", run)
    assert exc.value.code == "validation"


def test_record_run_accepts_item_key(w):
    to_claimed(w, "demo:A")
    w.store.record_run(sp(w), "demo:A", started(item_key="demo:A"))
    row = w.conn.execute(
        "SELECT payload FROM events WHERE key='demo:A' AND type='run_recorded'"
    ).fetchone()
    assert json.loads(row["payload"])["item_key"] == "demo:A"
    assert verify_integrity(w.conn) == []


def test_unknown_key_not_found(w):
    with pytest.raises(Refused) as exc:
        w.store.record_run(sp(w), "demo:NOPE", started())
    assert exc.value.code == "not_found"


def test_tampered_writer_model_is_integrity_problem(w):
    to_claimed(w, "demo:A")
    w.store.record_run(sp(w), "demo:A", started(writer={"model": "m1"}))
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE items SET writer_model='forged' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    problems = verify_integrity(w.conn)
    assert [(p["field"], p["expected"], p["got"]) for p in problems] == [
        ("writer_model", "m1", "forged")]


def test_old_database_gains_columns(tmp_path):
    from plangraph.keeper import db as kdb
    path = tmp_path / "old.db"
    kdb.open_db(path, create=True).close()
    raw = sqlite3.connect(str(path))
    for col in ("writer_model", "seat_models", "run_ids"):
        raw.execute(f"ALTER TABLE items DROP COLUMN {col}")
    raw.commit()
    raw.close()
    conn = kdb.open_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    assert {"writer_model", "seat_models", "run_ids"} <= cols
    conn.close()


def test_derived_done_reviewed_merged(w):
    from keeper_util import to_done
    to_claimed(w, "demo:A")
    w.store.record_run(sp(w), "demo:A", started(writer={"model": "m1"}))
    epoch = w.store._item("demo:A")["epoch"]
    w.store.add_evidence_router(w.p("router"), "demo:A", "commit+ci",
                                {"sha": "abc"})
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    from keeper_util import pass_review
    pass_review(w, "demo:A")
    info = w.store.show(w.p("reader"), "demo:A")
    assert info["reviewed"] is True and info["merged"] is True
    assert info["done_at"]
    assert info["created_at"]
    row = next(i for i in w.store.snapshot(w.p("reader"))["items"]
               if i["key"] == "demo:A")
    assert row["reviewed"] is True and row["merged"] is True
    assert row["done_at"] == info["done_at"]


def test_http_round_trip(w, tmp_path):
    to_claimed(w, "demo:A")
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(w.path), "snapshot": str(tmp_path / "snap.json"),
        "bind": "127.0.0.1:0", "reap_interval": 3600}), encoding="utf-8")
    import os
    os.chmod(cfg_path, 0o600)
    cfg = config.load_config(cfg_path)
    server = api.make_server(w.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        token_path = tmp_path / "sp.token"
        token_path.write_text(w.tok("spawner1"), encoding="utf-8")
        client = mcp.HttpKeeper(base, str(token_path), timeout=10)
        text, is_error = client.call(
            "POST", "record_run",
            {"key": "demo:A", "run": started(writer={"model": "mh"})})
        assert is_error is False, text
        assert json.loads(text)["writer_model"] == "mh"
        assert mcp._ROUTES["record_run"] == ("POST", "record_run")
        tools = {t["name"]: t for t in mcp.TOOLS}
        assert tools["record_run"]["inputSchema"]["required"] == ["key", "run"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
