"""Seat regression coverage for schema refusals and bounded telemetry work."""
import json

import pytest

from test_keeper_host_samples import DSN, world, sample, host_principal
from test_keeper_host_samples_http import srv
from test_keeper_api import call
from plangraph.keeper.errors import Refused

pytestmark = pytest.mark.skipif(not DSN, reason="needs Postgres")


@pytest.mark.parametrize("body", [[], "fixture", pytest.param(None, marks=pytest.mark.xfail(
    strict=True, reason="api.py treats JSON null as its 413 sentinel before dispatch")), 1])
def test_non_object_http(world, srv, body):
    _, minted = host_principal(world)
    status, _, _ = call(srv, "/v1/ingest_host_sample", method="POST",
                        body=json.dumps(body), token=minted["token"])
    assert status == 400
    assert world.conn.execute("SELECT count(*) FROM host_samples_raw").fetchone()[0] == 0


@pytest.mark.parametrize("start,end,resolution", [
    ("bad", "1", "raw"), ("0", "bad", "raw"), ("2", "1", "raw"),
    ("0", "86401", "raw"), ("0", "2592001", "1m")])
def test_range_http_refusals(world, srv, start, end, resolution):
    path = (f"/v1/host_samples_range?host_id=host-a&from={start}&to={end}"
            f"&resolution={resolution}")
    assert call(srv, path, token=world.tok("reader"))[0] == 400


@pytest.mark.parametrize("resolution", ["raw", "1m", "1h"])
def test_range_row_cap(world, resolution):
    who, _ = host_principal(world)
    world.store.ingest_host_sample(who, sample())
    start = world.clock()
    # Dense fixture rows exercise the response cap independently of bucket width.
    world.conn.execute(
        f"INSERT INTO host_samples_{resolution} "
        "(host_id,received_at,seq,sample,sample_count) "
        "SELECT 'host-a', ? + n, n, ?, 1 FROM generate_series(1,10001) n",
        (start, json.dumps(sample())))
    result = world.store.host_samples_range(world.p("reader"), "host-a",
                                            start + 1, start + 10002, resolution)
    assert result["truncated"] is True
    assert len(result["rows"]) == 10000
    assert result["rows"][0]["received_at"] == start + 1


def test_revoke_unknown_and_repeat(world):
    with pytest.raises(Refused, match="not_found"):
        world.store.revoke_host_token(world.p("router"), 999)
    _, minted = host_principal(world)
    for _ in range(2):
        assert world.store.revoke_host_token(world.p("router"), minted["id"]) == {"id": minted["id"]}


def test_store_null_refused(world):
    who, _ = host_principal(world)
    with pytest.raises(Refused, match="validation"):
        world.store.ingest_host_sample(who, None)


@pytest.mark.parametrize("source,target,width", [("raw", "1m", 60), ("1m", "1h", 3600)])
def test_rollup_recent_bound(world, source, target, width):
    from plangraph.keeper.host_samples_rollup import rollup
    who, _ = host_principal(world)
    world.store.ingest_host_sample(who, sample())
    start = world.clock() // width * width
    if source == "1m":
        world.conn.execute("INSERT INTO host_samples_1m SELECT * FROM host_samples_raw")
    world.conn.execute(
        f"INSERT INTO host_samples_{source} "
        "(host_id,received_at,seq,sample,sample_count) VALUES(?,?,?,?,1)",
        ("host-a", start + 4 * width, 2, json.dumps(sample(2))))
    now = start + 6 * width
    rollup(world.conn, source, target, width, now)
    before = world.conn.execute(
        f"SELECT xmin::text FROM host_samples_{target} WHERE received_at=?", (start,)).fetchone()[0]
    # A new transaction makes rewriting an old bucket observable through xmin.
    rollup(world.conn, source, target, width, now)
    after = world.conn.execute(
        f"SELECT xmin::text FROM host_samples_{target} WHERE received_at=?", (start,)).fetchone()[0]
    assert after == before
