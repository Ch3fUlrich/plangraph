"""HTTP API: auth, refusals, forged actor ignored, body cap, health, text."""
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from keeper_util import World, seed
from plangraph.keeper import api, config


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])])
    yield world
    world.close()


def make_cfg(tmp_path, world=None):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path) if world else "k.db",
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0",
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


@pytest.fixture
def srv(w, tmp_path):
    cfg = make_cfg(tmp_path, w)
    server = api.make_server(w.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(srv, path, *, method="GET", body=None, token=None, raw=False):
    url = srv.base_url + path
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        if isinstance(data, str):
            data = data.encode()
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw_body = resp.read()
            return resp.status, resp.headers.get("Content-Type", ""), raw_body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def jbody(raw):
    return json.loads(raw.decode())


def test_health_is_open(srv):
    status, _, raw = call(srv, "/v1/health")
    assert status == 200
    assert jbody(raw)["status"] == "ok"


def test_auth_is_required(srv):
    status, _, raw = call(srv, "/v1/whoami")
    assert status == 401
    assert jbody(raw)["error"] == "forbidden"


def test_unknown_token_is_401(srv):
    status, _, raw = call(srv, "/v1/whoami", token="nope")
    assert status == 401


def test_refused_maps_to_4xx_with_code(srv, w):
    status, _, raw = call(srv, "/v1/claim", method="POST",
                          body={"key": "demo:A"}, token=w.tok("reader"))
    assert 400 <= status < 500
    assert jbody(raw)["error"] == "forbidden"


def test_forged_actor_in_body_is_ignored(srv, w):
    status, _, raw = call(
        srv, "/v1/claim", method="POST",
        body={"key": "demo:A", "actor": "router", "role": "router"},
        token=w.tok("workerA"))
    assert status == 200, raw
    assert jbody(raw)["key"] == "demo:A"
    status, _, raw = call(srv, "/v1/show?key=demo:A&fmt=json", token=w.tok("reader"))
    assert jbody(raw)["claimed_by"] == "workerA"


def test_the_token_principal_is_used_not_the_body(srv, w):
    # set_priority is router-only; a worker token must be refused even though
    # the body claims to be the router.
    status, _, raw = call(srv, "/v1/set_priority", method="POST",
                          body={"key": "demo:A", "n": 5, "role": "router"},
                          token=w.tok("workerA"))
    assert status == 403
    assert jbody(raw)["error"] == "forbidden"


def test_oversize_body_is_413(srv, w):
    big = b"x" * (api.MAX_BODY + 1)
    status, _, raw = call(srv, "/v1/claim", method="POST", body=big,
                          token=w.tok("workerA"))
    assert status == 413
    assert jbody(raw)["error"] == "too_large"


def test_ready_is_compact_text_by_default(srv, w):
    status, ctype, raw = call(srv, "/v1/ready", token=w.tok("reader"))
    assert status == 200
    assert "text/plain" in ctype
    text = raw.decode()
    assert "demo:A" in text and "demo:B" not in text
    assert "{" not in text


def test_ready_json_when_fmt_json(srv, w):
    status, ctype, raw = call(srv, "/v1/ready?fmt=json", token=w.tok("reader"))
    assert status == 200
    assert "application/json" in ctype
    keys = [it["key"] for it in jbody(raw)["items"]]
    assert keys == ["demo:A"]


def test_show_text_and_json(srv, w):
    _, ctype, raw = call(srv, "/v1/show?key=demo:A", token=w.tok("reader"))
    assert "text/plain" in ctype
    _, ctype, raw = call(srv, "/v1/show?key=demo:A&fmt=json",
                         token=w.tok("reader"))
    assert "application/json" in ctype
    assert jbody(raw)["key"] == "demo:A"


def test_unexpected_exception_is_500_without_internals(srv, w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SECRET-INTERNAL-DETAIL")
    monkeypatch.setattr(w.store, "waves", boom)
    status, _, raw = call(srv, "/v1/waves", token=w.tok("reader"))
    assert status == 500
    text = raw.decode()
    assert "SECRET-INTERNAL-DETAIL" not in text
    assert jbody(raw)["error"] == "internal"


def test_snapshot_rewritten_after_write(srv, w, tmp_path):
    snap = tmp_path / "plan.snapshot.json"
    assert not snap.exists()
    call(srv, "/v1/claim", method="POST", body={"key": "demo:A"},
         token=w.tok("l1main"))
    srv.flush_snapshot()          # KEEPER-SNAPSHOT-PG: the file is rebuilt after writes go quiet
    assert snap.exists()
    data = json.loads(snap.read_text())
    assert data["head"] and {it["key"] for it in data["items"]} >= {"demo:A"}
    # atomic: no temp leftovers
    assert not list(tmp_path.glob("*.tmp*"))


def test_server_start_stop_loop_is_clean(w, tmp_path):
    """Regression: ``Reaper`` shadowed ``threading.Thread._stop``.

    Starting and stopping 20 servers must never raise from the reaper's
    ``join``; before the rename to ``_halt`` this was ``TypeError: 'Event'
    object is not callable`` on every stopped thread.
    """
    cfg = make_cfg(tmp_path, w)
    for _ in range(20):
        server = api.make_server(w.store, cfg, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_auth_failure_is_audited_never_token(srv, w, monkeypatch):
    """A rejected token is audited by reason only, and the token never leaks."""
    calls = []
    monkeypatch.setattr(w.store, "audit_auth_failure",
                        lambda reason: calls.append(reason), raising=False)
    token = "not-a-real-token"
    status, _, _ = call(srv, "/v1/whoami", token=token)
    assert status == 401
    assert calls == ["unknown or revoked token"]
    status, _, _ = call(srv, "/v1/whoami")  # missing Authorization header
    assert status == 401
    assert calls[-1] == "missing token"
    assert all(token not in reason for reason in calls)


def test_apply_ops_refuses_unknown_body_keys_and_advertises_dry_run(srv, w):
    # D-1498/D-2760: an ignored body key once turned a dry run into a real apply.
    status, _, raw = call(srv, "/v1/apply_ops", method="POST",
                          body={"ops": [], "dryrun": True}, token=w.tok("router"))
    assert status == 400 and "dryrun" in raw.decode()
    status, _, raw = call(srv, "/v1/whoami", token=w.tok("router"))
    assert "apply_ops.dry_run" in jbody(raw)["features"]
