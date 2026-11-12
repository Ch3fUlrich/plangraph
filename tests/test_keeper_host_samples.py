"""HOST-SAMPLES-API: Postgres ingest, isolation, and server-side freshness."""
import os

import pytest

from keeper_util import World
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused

DSN = os.environ.get("PLANGRAPH_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PLANGRAPH_TEST_PG_DSN unset: needs Postgres 16")


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, backend="postgres", dsn=DSN)
    try:
        yield w
    finally:
        w.close()


def sample(seq=1, started=1):
    pair = {"mean": 20, "max": 30}
    return {"host_id": "host-a", "seq": seq, "agent_started_at": started,
            "cpu_pct": pair, "mem_available_bytes": pair,
            "swap_in_bytes_s": pair, "disk_free_bytes": pair,
            "gpus": [{"index": 0, "util_pct": pair, "mem_free_bytes": pair,
                      "mem_total_bytes": pair},
                     {"index": 1, "util_pct": None, "mem_free_bytes": None,
                      "mem_total_bytes": None}],
            "agent_processes": {k: 1 for k in ("claude", "opencode", "qodercli", "agy")}}


def host_principal(w):
    minted = w.store.mint_host_token(w.p("router"), "host-a")
    return auth.authenticate(w.conn, minted["token"]), minted


def test_pg_ingest_server_clock_and_nullable_multiple_gpus(world):
    who, minted = host_principal(world)
    out = world.store.ingest_host_sample(who, sample())
    assert out["received_at"] == world.clock()
    rows = world.store.host_samples(world.p("reader"))
    assert len(rows) == 1 and rows[0]["state"] == "fresh"
    assert rows[0]["sample"] == sample()
    row = world.conn.execute("SELECT token_sha256 FROM host_sample_tokens").fetchone()
    assert row[0] == auth.hash_token(minted["token"])
    assert minted["token"] not in str(tuple(row))


@pytest.mark.parametrize("field,value", [
    ("received_at", 0), ("paths", ["fixture-path"]), ("args", ["fixture-arg"]),
    ("env", {"FIXTURE": "value"}), ("text", "fixture-text")])
def test_pg_payload_refused_not_stored(world, field, value):
    who, _ = host_principal(world)
    body = sample()
    body[field] = value
    with pytest.raises(Refused):
        world.store.ingest_host_sample(who, body)
    assert world.conn.execute("SELECT count(*) FROM host_samples_raw").fetchone()[0] == 0


@pytest.mark.parametrize("location", ["cpu_pct", "gpus", "agent_processes", "rule_overrides"])
def test_pg_nested_free_text_refused(world, location):
    who, _ = host_principal(world)
    body = sample()
    if location == "gpus":
        body[location][0]["path"] = "fixture-text"
    else:
        body[location] = {"text": "fixture-text"}
    with pytest.raises(Refused):
        world.store.ingest_host_sample(who, body)
    assert world.conn.execute("SELECT count(*) FROM host_samples_raw").fetchone()[0] == 0


def test_pg_host_token_only_own_ingest_revocation(world):
    who, minted = host_principal(world)
    body = sample()
    body["host_id"] = "host-b"
    with pytest.raises(Refused):
        world.store.ingest_host_sample(who, body)
    with pytest.raises(Refused):
        world.store.host_samples(who)
    with pytest.raises(Refused):
        world.store.create_items(who, [])
    world.store.revoke_host_token(world.p("l1main"), minted["id"])
    with pytest.raises(Refused):
        auth.authenticate(world.conn, minted["token"])
    with pytest.raises(Refused):
        world.store.ingest_host_sample(who, sample())


@pytest.mark.parametrize("name", ["router", "l1main", "reader", "workerA", "system"])
def test_pg_other_principal_namespaces_cannot_ingest(world, name):
    with pytest.raises(Refused):
        world.store.ingest_host_sample(world.p(name), sample())


def test_pg_freshness_gaps_restarts_and_range(world, monkeypatch):
    monkeypatch.setenv("PG_HOST_SAMPLE_S", "60")
    who, _ = host_principal(world)
    start = world.clock()
    world.store.ingest_host_sample(who, sample())
    world.clock.advance(60)
    world.store.ingest_host_sample(who, sample(4))
    latest = world.store.host_samples(world.p("reader"))[0]
    assert latest["seq_gap"] == 2 and not latest["restarted"]
    world.clock.advance(180)
    assert world.store.host_samples(world.p("reader"))[0]["state"] == "stale"
    world.clock.advance(420)
    assert world.store.host_samples(world.p("reader"))[0]["state"] == "down"
    world.store.ingest_host_sample(who, sample(1, 2))
    latest = world.store.host_samples(world.p("reader"))[0]
    assert latest["restarted"] and latest["seq_gap"] == 0
    rows = world.store.host_samples_range(world.p("reader"), "host-a", start,
                                          start + 61, "raw")
    assert [r["sample"]["seq"] for r in rows] == [1, 4]


def test_pg_non_admin_cannot_mint_or_revoke(world):
    for name in ("workerA", "reader"):
        with pytest.raises(Refused):
            world.store.mint_host_token(world.p(name), "host-a")
        with pytest.raises(Refused):
            world.store.revoke_host_token(world.p(name), 1)


def test_sqlite_host_sample_verbs_refused_cleanly(tmp_path):
    w = World(tmp_path, backend="sqlite")
    try:
        with pytest.raises(Refused, match="Postgres"):
            w.store.host_samples(w.p("reader"))
        with pytest.raises(Refused, match="Postgres"):
            w.store.mint_host_token(w.p("router"), "host-a")
    finally:
        w.close()
