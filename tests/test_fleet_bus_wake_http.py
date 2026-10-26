"""RT-P1-BUS-WAKE (http): the synthetic wake POST and the keeper long-poll client.

No socket is ever opened in these tests: both seams take an injected transport
and assert the exact wire shape.  A synthetic message is part of the
conversation, not a user prompt, so the wake never hits `/prompt`.  The
redirect tests at the end are the exception: redirect policy lives inside the
transport, so they run loopback servers (G3-7).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plangraph.fleet_bus.wake import (
    DEFAULT_TEXT,
    PollFailed,
    SessionTarget,
    WakeBusy,
    WakeHook,
    WakeRefused,
)
from plangraph.fleet_bus.wake_http import (
    OPENCODE_SYNTHETIC_PATH,
    KeeperMessagesPoll,
    SyntheticSink,
)

EPOCH = 1_700_000_000.0
OC_BASE = "http://oc.example.test:4096"
KEEPER_BASE = "http://keeper.example.test:8765"
ROUTER = SessionTarget("ses_r1", OC_BASE)
CLAUDE = SessionTarget("ses_c1", "http://cc.example.test:4097", kind="claude")

#: The exact bytes a wake must carry over the wire (asserted on every hop).
WAKE_BODY = json.dumps({"text": DEFAULT_TEXT, "resume": True}).encode("utf-8")


def recorder(status=200, raw=b"{}"):
    """A fake transport: captures (url, body, timeout, headers), never connects."""
    calls = []

    def post(url, body, timeout, headers):
        calls.append((url, body, timeout, headers))
        return status, raw

    return post, calls


# ------------------------------------------------------------- synthetic wake

def test_opencode_wake_posts_a_synthetic_message_not_a_prompt():
    post, calls = recorder()
    SyntheticSink(post=post, authorization="Bearer tok").wake(ROUTER, DEFAULT_TEXT)

    url, body, timeout, headers = calls[0]
    assert url == OC_BASE + OPENCODE_SYNTHETIC_PATH.format(session="ses_r1")
    assert body == {"text": DEFAULT_TEXT, "resume": True}
    assert "/prompt" not in url          # a visible user bubble is the defect
    assert timeout > 0
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer tok"


def test_opencode_path_template_is_the_documented_one():
    assert OPENCODE_SYNTHETIC_PATH == "/api/session/{session}/synthetic"
    assert SyntheticSink().paths["opencode"] == OPENCODE_SYNTHETIC_PATH


@pytest.mark.parametrize("status,expected", [
    (200, None), (204, None),
    (404, WakeRefused),        # the session does not exist: refuse, never crash
    (409, WakeBusy), (423, WakeBusy), (429, WakeBusy), (503, WakeBusy),
    (500, WakeBusy), (400, WakeBusy),  # anything transient waits for the next tick
])
def test_http_status_decides_busy_versus_refused(status, expected):
    post, _ = recorder(status=status)
    sink = SyntheticSink(post=post)
    if expected is None:
        sink.wake(ROUTER, DEFAULT_TEXT)
    else:
        with pytest.raises(expected):
            sink.wake(ROUTER, DEFAULT_TEXT)


def test_transport_failure_is_busy_so_the_wake_is_never_dropped():
    def post(url, body, timeout, headers):
        raise OSError("connection refused")

    with pytest.raises(WakeBusy):
        SyntheticSink(post=post).wake(ROUTER, DEFAULT_TEXT)


def test_claude_session_uses_its_configured_synthetic_inbox_path():
    """Claude sessions keep their own synthetic/inbox path; only the path is
    configured, the request body keeps the reference shape."""
    post, calls = recorder()
    sink = SyntheticSink(paths={"claude": "/api/session/{session}/inbox"}, post=post)
    sink.wake(CLAUDE, DEFAULT_TEXT)

    url, body, _timeout, _headers = calls[0]
    assert url == "http://cc.example.test:4097/api/session/ses_c1/inbox"
    assert body == {"text": DEFAULT_TEXT, "resume": True}


def test_unconfigured_session_kind_or_endpoint_is_refused():
    with pytest.raises(WakeRefused):            # no claude path configured
        SyntheticSink(post=recorder()[0]).wake(CLAUDE, DEFAULT_TEXT)
    with pytest.raises(WakeRefused):            # target carries no endpoint
        SyntheticSink(post=recorder()[0]).wake(SessionTarget("ses_r1", ""), DEFAULT_TEXT)


def test_unknown_session_answered_404_is_refused_through_the_hook(tmp_path):
    """Delivery layer of the unknown-session vector: 404 -> refused + logged,
    the cursor still advances and nothing retries forever."""
    post, _ = recorder(status=404)
    sink = SyntheticSink(post=post)
    log = []
    hook = WakeHook({"router": ROUTER}, sink, clock=lambda: EPOCH, log=log.append)

    assert hook.feed([{"seq": 1, "from": "worker", "to": "router"}]) == ["refused"]
    assert hook.cursor == 1
    assert hook.state.pending == {}
    assert any("refused" in message for message in log)
    assert any("ses_r1" in entry or "router" in entry for entry in hook.refused)


# ------------------------------------------------------------ keeper long-poll

def test_keeper_messages_poll_request_shape():
    gets = []

    def get(url, timeout, headers):
        gets.append((url, timeout, headers))
        return 200, json.dumps({"rows": [{"seq": 3, "to": "router"}]}).encode("utf-8")

    poll = KeeperMessagesPoll(KEEPER_BASE + "/", "tok", get=get)
    rows = poll(12, 30000)

    assert rows == [{"seq": 3, "to": "router"}]
    url, timeout, headers = gets[0]
    assert url == KEEPER_BASE + "/v1/messages?since=12&wait_ms=30000"
    assert headers == {"Authorization": "Bearer tok"}
    assert timeout > 30                  # the socket timeout covers the window


@pytest.mark.parametrize("status,raw", [
    (503, b'{"error": {"code": "busy"}}'),
    (200, b"not json"),
    (200, b'{"rows": {}}'),
    (200, b'{"error": {"code": "validation"}}'),
])
def test_keeper_messages_poll_failures_are_transient(status, raw):
    """A failing read raises `PollFailed`; `WakeHook.run` logs it and retries,
    so a keeper hiccup never stops the wake loop."""
    def get(url, timeout, headers):
        return status, raw

    with pytest.raises(PollFailed):
        KeeperMessagesPoll(KEEPER_BASE, "tok", get=get)(0, 30000)


def test_keeper_messages_poll_requires_an_endpoint_and_a_token():
    with pytest.raises(ValueError):
        KeeperMessagesPoll("", "tok")
    with pytest.raises(ValueError):
        KeeperMessagesPoll(KEEPER_BASE, "")


# --------------------------------------------------------- real redirects (G3-7)

def _url(server, path):
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def _server(hits, redirect_to=None, redirect_code=302):
    """Start a loopback server that records ``(method, path, Authorization, body)``.

    ``redirect_to`` answers each request with a ``redirect_code`` to it: a path is resolved
    against this server's own origin (same-origin hop), a full URL is used as
    given (cross-origin hop).  A same-origin hop redirects exactly once -- the
    target path itself lands with a 204 -- so a redirect can never loop.
    """
    target_path = None if (redirect_to and ":" in redirect_to) else redirect_to

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            hits.append((self.command, self.path,
                         self.headers.get("Authorization"), body))
            if redirect_to and self.path != target_path:
                location = (redirect_to if "://" in redirect_to
                            else f"http://{self.headers['Host']}{redirect_to}")
                self.send_response(redirect_code)
                self.send_header("Location", location)
                self.end_headers()
            else:
                self.send_response(204)
                self.end_headers()

        do_GET = do_POST = _answer

        def log_message(self, *_args):   # keep the test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _close(*servers):
    for server in servers:
        server.shutdown()
        server.server_close()


def test_a_same_origin_redirect_is_followed_with_the_bearer_token_intact():
    """A wake behind a same-origin 307 redirect still lands as a POST, token and all."""
    hits = []
    edge = _server(hits, redirect_to="/synthetic/final", redirect_code=307)
    try:
        sink = SyntheticSink(authorization="Bearer tok")
        sink.wake(SessionTarget("ses_x", _url(edge, "")), DEFAULT_TEXT)
    finally:
        _close(edge)
    assert hits == [("POST", "/api/session/ses_x/synthetic", "Bearer tok", WAKE_BODY),
                    ("POST", "/synthetic/final", "Bearer tok", WAKE_BODY)]


def test_a_cross_origin_redirect_is_refused_and_never_forwards_authorization():
    """G3-7: the bearer token never crosses an origin boundary.

    The 302 to another host is not followed, so the other origin sees NO
    request at all; the wake reports busy (retried later, never dropped) and
    the long-poll would keep its cursor instead of leaking the header.
    """
    edge_hits, other_hits = [], []
    other = _server(other_hits)
    edge = _server(edge_hits, redirect_to=_url(other, "/steal"))
    try:
        sink = SyntheticSink(authorization="Bearer tok")
        with pytest.raises(WakeBusy):
            sink.wake(SessionTarget("ses_x", _url(edge, "")), DEFAULT_TEXT)
    finally:
        _close(edge, other)
    assert edge_hits == [("POST", "/api/session/ses_x/synthetic", "Bearer tok", WAKE_BODY)]  # earned it
    assert other_hits == []                                                               # foreign: nothing


@pytest.mark.parametrize("code", [301, 302, 303])
def test_a_302_on_a_wake_is_busy_never_a_bodyless_get(code):
    """I5: a wake POST is never downgraded.

    urllib re-issues a redirected 301/302/303 POST as a body-less GET (the
    payload vanishes); busy instead, and the final path saw zero requests.
    """
    hits = []
    edge = _server(hits, redirect_to="/synthetic/final", redirect_code=code)
    try:
        sink = SyntheticSink(authorization="Bearer tok")
        with pytest.raises(WakeBusy):
            sink.wake(SessionTarget("ses_x", _url(edge, "")), DEFAULT_TEXT)
    finally:
        _close(edge)
    assert hits == [("POST", "/api/session/ses_x/synthetic", "Bearer tok", WAKE_BODY)]
    assert not any(hit[0] == "GET" for hit in hits)   # final server: 0 GETs


@pytest.mark.parametrize("code", [307, 308])
def test_a_307_or_308_is_followed_as_post_with_body_and_bearer(code):
    """I5: 307/308 preserve the method, so the hop is followed as a POST."""
    hits = []
    edge = _server(hits, redirect_to="/synthetic/final", redirect_code=code)
    try:
        sink = SyntheticSink(authorization="Bearer tok")
        sink.wake(SessionTarget("ses_x", _url(edge, "")), DEFAULT_TEXT)
    finally:
        _close(edge)
    assert hits == [("POST", "/api/session/ses_x/synthetic", "Bearer tok", WAKE_BODY),
                    ("POST", "/synthetic/final", "Bearer tok", WAKE_BODY)]


def test_a_permanent_cross_origin_redirect_ends_refused(caplog):
    """I6: two refusals stay busy; the third gives up as WakeRefused, logged once."""
    edge_hits, other_hits = [], []
    other = _server(other_hits)
    edge = _server(edge_hits, redirect_to=_url(other, "/steal"))
    sink = SyntheticSink(authorization="Bearer tok")       # cross_origin_max=3
    target = SessionTarget("ses_x", _url(edge, ""))
    try:
        with pytest.raises(WakeBusy):
            sink.wake(target, DEFAULT_TEXT)
        with pytest.raises(WakeBusy):
            sink.wake(target, DEFAULT_TEXT)
        with pytest.raises(WakeRefused) as refused:
            sink.wake(target, DEFAULT_TEXT)
        with pytest.raises(WakeRefused):
            sink.wake(target, DEFAULT_TEXT)                # no second log line
    finally:
        _close(edge, other)
    assert "cross-origin redirect" in str(refused.value)
    assert other_hits == []                                # foreign: never called
    assert sum("cross-origin" in rec.getMessage() for rec in caplog.records) == 1


def test_a_regular_outcome_resets_the_cross_origin_refusal_count():
    """I6: only *consecutive* refusals count; any other outcome resets them."""
    other, plain = _server([]), _server([])
    edge = _server([], redirect_to=_url(other, "/steal"))
    sink = SyntheticSink(authorization="Bearer tok")
    target = SessionTarget("ses_x", _url(edge, ""))
    try:
        for _ in range(2):
            with pytest.raises(WakeBusy):
                sink.wake(target, DEFAULT_TEXT)
        sink.wake(SessionTarget("ses_x", _url(plain, "")), DEFAULT_TEXT)  # resets
        for _ in range(2):
            with pytest.raises(WakeBusy):   # restarted: never reaches refused
                sink.wake(target, DEFAULT_TEXT)
    finally:
        _close(edge, other, plain)


def test_a_malformed_redirect_port_is_busy_not_valueerror():
    """I7: a bad ``Location`` port can never escape as ValueError."""
    hits = []
    edge = _server(hits, redirect_to="http://h:99999/steal")
    try:
        sink = SyntheticSink(authorization="Bearer tok")
        with pytest.raises(WakeBusy):
            sink.wake(SessionTarget("ses_x", _url(edge, "")), DEFAULT_TEXT)
    finally:
        _close(edge)
    assert hits == [("POST", "/api/session/ses_x/synthetic", "Bearer tok", WAKE_BODY)]


def test_a_malformed_redirect_port_fails_the_poll_cleanly():
    """I7: the same bad port fails the keeper poll as PollFailed, never ValueError."""
    edge = _server([], redirect_to="http://h:99999/steal")
    try:
        poll = KeeperMessagesPoll(_url(edge, ""), "tok")
        with pytest.raises(PollFailed):
            poll(0, 30000)
    finally:
        _close(edge)
