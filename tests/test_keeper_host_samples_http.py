"""Postgres host samples must be reachable, and isolated, over HTTP."""
import json
import threading

import pytest

from test_keeper_host_samples import DSN, world, sample, host_principal
from test_keeper_api import call, make_cfg
from keeper_util import seed
from plangraph.keeper import api

pytestmark = pytest.mark.skipif(not DSN, reason="PLANGRAPH_TEST_PG_DSN unset: needs Postgres 16")


@pytest.fixture
def srv(world, tmp_path):
    server = api.make_server(world.store, make_cfg(tmp_path, world), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Reuse the bound address rather than embedding a deployment address.
    address, port = server.server_address
    server.base_url = f"http://{address}:{port}"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_ingest_latest_and_range(world, srv):
    _, minted = host_principal(world)
    status, _, body = call(srv, "/v1/ingest_host_sample", method="POST",
                           token=minted["token"], body=sample())
    assert status == 200
    assert json.loads(body)["received_at"] == world.clock()
    status, _, body = call(srv, "/v1/host_samples", token=world.tok("reader"))
    assert status == 200
    rows = json.loads(body)
    assert rows[0]["sample"] == sample()
    assert rows[0]["state"] == "fresh"
    start = int(world.clock())
    status, _, body = call(srv, f"/v1/host_samples_range?host_id=host-a&from={start}&to={start + 1}&resolution=raw",
                           token=world.tok("reader"))
    assert status == 200
    assert json.loads(body)[0]["sample"] == sample()


def test_http_host_token_scope_and_revocation(world, srv):
    _, minted = host_principal(world)
    token = minted["token"]
    for path, method, body in [
        ("/v1/host_samples", "GET", None),
        ("/v1/host_samples_range?host_id=host-a&from=0&to=1&resolution=raw", "GET", None),
        ("/v1/create_items", "POST", {"items": []}),
        ("/v1/ingest_host_sample", "POST", dict(sample(), host_id="host-b")),
    ]:
        assert call(srv, path, method=method, body=body, token=token)[0] == 403
    world.store.revoke_host_token(world.p("router"), minted["id"])
    assert call(srv, "/v1/ingest_host_sample", method="POST", body=sample(), token=token)[0] == 403


@pytest.mark.parametrize("namespace", ["principal", "run", "oversight"])
def test_http_other_token_namespaces_cannot_ingest(world, srv, namespace):
    if namespace == "principal":
        token = world.tok("router")
    elif namespace == "run":
        from test_keeper_run_tokens import FakeRuns
        seed(world, [("A", [])], delegate=True)
        world.store.ctx = {"runs": FakeRuns()}
        token = world.store.mint_run_token(world.p("router"), "demo:A", "writer", "w1")["token"]
    else:
        # An unknown token in the oversight namespace must not fall through.
        from plangraph.keeper.auth import OVERSIGHT_PREFIX
        token = OVERSIGHT_PREFIX + "fixture-token"
    assert call(srv, "/v1/ingest_host_sample", method="POST", body=sample(), token=token)[0] == 403


def test_http_payload_refused_and_not_stored(world, srv):
    _, minted = host_principal(world)
    status, _, _ = call(srv, "/v1/ingest_host_sample", method="POST",
                         body=dict(sample(), paths=["fixture-path"]), token=minted["token"])
    assert status == 400
    assert world.conn.execute("SELECT count(*) FROM host_samples_raw").fetchone()[0] == 0


def test_http_valid_minted_oversight_cannot_ingest(world, srv, monkeypatch):
    from test_keeper_oversight_scope import live
    token, principal = live(world, monkeypatch)
    assert principal.role == 'oversight'
    assert call(srv, '/v1/ingest_host_sample', method='POST', body=sample(),
                token=token)[0] == 403
    assert world.conn.execute('SELECT count(*) FROM host_samples_raw').fetchone()[0] == 0
