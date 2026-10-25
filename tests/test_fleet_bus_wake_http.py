"""RT-P1-BUS-WAKE (http): the synthetic wake POST and the keeper long-poll client.

No socket is ever opened in these tests: both seams take an injected transport
and assert the exact wire shape.  A synthetic message is part of the
conversation, not a user prompt, so the wake never hits `/prompt`.
"""
from __future__ import annotations

import json

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
