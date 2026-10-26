"""RT-P1-BUS-WAKE: the wake daemon entrypoint turns one real keeper row into one
wake, and a bad config fails closed (one safe stderr line, exit 2, zero requests).
Both tests drive the real entrypoint `main` against a real keeper
(`keeper_util.World` over `door_util.Doors`) with a stub session server recording every request.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from door_util import Doors
from keeper_util import World
from plangraph.fleet_bus.wake import main

BAD_PREFIX = "fleet-wake: bad config:"  # the fail-closed line's one prefix
FAKE_TOKEN = "wake-tok-1"  # stand-in bearer for the config-error cases


class Stub:
    """A stub session HTTP server recording (method, path, body) per request."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                outer.requests.append((self.command, self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = _handle

            def log_message(self, *args):
                pass  # keep pytest output clean

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def write_config(path, *, keeper_url, token_file, session_base, state_file, sessions):
    """Write one daemon config file (the shape `load_config` documents)."""
    path.write_text(json.dumps({
        "keeper_url": keeper_url, "token_file": token_file,
        "session_base": session_base, "state_file": state_file,
        "sessions": sessions}), encoding="utf-8")


@pytest.mark.parametrize("case", ["missing file", "bad json", "no sessions",
                                  "missing token file", "empty token"])
def test_daemon_refuses_a_bad_config(tmp_path, case, capsys):
    """I8b: every bad config fails closed -- exit 2, one safe stderr line (never
    the token, never file contents) and zero requests on any socket."""
    stub = Stub()
    try:
        cfg_path = tmp_path / "fleet-wake.json"
        tok_path = tmp_path / "keeper.token"
        tok_path.write_text(FAKE_TOKEN, encoding="utf-8")
        if case == "missing token file":
            tok_path.unlink()
        elif case == "empty token":
            tok_path.write_text(" \n", encoding="utf-8")
        if case == "bad json":
            cfg_path.write_text("{not json", encoding="utf-8")
        elif case != "missing file":
            write_config(cfg_path, keeper_url="http://127.0.0.1:9",
                         token_file=str(tok_path), session_base=stub.url,
                         state_file=str(tmp_path / "wake.state"),
                         sessions={} if case == "no sessions"
                         else {"router": {"session": "ses_r1", "kind": "opencode"}})
        code = main(["--config", str(cfg_path)], stop=threading.Event())
    finally:
        stub.close()
    err = capsys.readouterr().err
    assert code == 2
    lines = err.splitlines()
    assert len(lines) == 1 and lines[0].startswith(BAD_PREFIX), err
    assert FAKE_TOKEN not in err
    assert stub.requests == []


def test_daemon_wakes_the_configured_session_from_the_real_keeper(tmp_path):
    """I8a: one report row reaches the daemon's long-poll and produces exactly one
    wake POST into the configured session, end to end through `main`; `main`
    returns 0 once the stop event is set."""
    world = World(tmp_path)
    doors = Doors(world, tmp_path)
    stub = Stub()
    stop, result = threading.Event(), {}
    try:
        tok_path = tmp_path / "keeper.token"
        tok_path.write_text(world.tok("router") + "\n", encoding="utf-8")  # stripped
        cfg_path = tmp_path / "fleet-wake.json"
        write_config(cfg_path, keeper_url=doors.url, token_file=str(tok_path),
                     session_base=stub.url, state_file=str(tmp_path / "wake.state"),
                     sessions={"router": {"session": "ses_r1", "kind": "opencode"}})
        thread = threading.Thread(
            target=lambda: result.update(rc=main(["--config", str(cfg_path)], stop=stop)),
            daemon=True)
        thread.start()
        status, out = doors.http("workerA", "POST", "/v1/post_message",
                                 {"from": "workerA", "to": "router",
                                  "body": {"op": "report"}})
        assert status == 200, out
        deadline = time.monotonic() + 60
        while not stub.requests and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stub.requests, "no wake reached the session server within 60 s"
        assert stub.requests == [("POST", "/api/session/ses_r1/synthetic",
                                  {"text": "fleet-wake", "resume": True})]
    finally:
        stop.set()
        # One row the hook ignores (router is a quiet sender) breaks a still
        # blocked long-poll, so the join is quick and the requests stay at one.
        doors.http("router", "POST", "/v1/post_message",
                   {"from": "router", "to": "router", "body": {"op": "noop"}})
        thread.join(timeout=15)
        stub.close()
        doors.close()
        world.close()
    assert result.get("rc") == 0
    assert not thread.is_alive()
