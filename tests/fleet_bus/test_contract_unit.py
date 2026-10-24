"""Stateful contract expectations. Validation owns auth/rate/dedupe state.

Transport-only READ authentication, query limit and response serialization are
specified here as required public helpers for the subsequent implementation.
"""
import importlib
import json
from collections import OrderedDict
from unittest.mock import patch

import pytest

SECRET = b"test-secret-not-real"
NOW = 1700000000


def post(**overrides):
    return dict({"from": "sender", "to": "receiver", "ts": NOW,
                 "nonce": "nonce-1", "msg_id": "message-1", "text": "hello"}, **overrides)


def fleet_state(contract):
    state = contract.new_state()
    assert isinstance(state.seen, OrderedDict)
    assert isinstance(state.msg_ids, dict)
    assert isinstance(state.limiter, contract.RateLimiter)
    return state


@pytest.fixture
def c():
    return importlib.import_module("plangraph.fleet_bus.contract")


def send(c, state, envelope=None, now=NOW, sig=None):
    envelope = envelope or post()
    return c.validate_post(lambda kid: SECRET, json.dumps(envelope).encode(),
                           sig if sig is not None else c.sign(SECRET, c.canonical_post(envelope)), now, state)


def test_bad_signature_does_not_burn_nonce(c):
    state = fleet_state(c)
    with pytest.raises(c.BusError) as err:
        send(c, state, sig="0" * 64)
    assert err.value.code == "bad_signature"
    assert not state.seen
    send(c, state)
    with pytest.raises(c.BusError) as err:
        send(c, state)
    assert err.value.code == "replayed_nonce"


def test_signature_uses_constant_time_compare(c):
    with patch.object(c.hmac, "compare_digest", wraps=c.hmac.compare_digest) as compare:
        send(c, fleet_state(c))
    assert compare.called


def test_rate_is_per_sender_and_rolls_at_sixty_seconds(c):
    state = fleet_state(c)
    for n in range(30):
        send(c, state, post(nonce=str(n), msg_id=str(n)))
    with pytest.raises(c.BusError) as err:
        send(c, state, post(nonce="over", msg_id="over"), now=NOW + 59)
    assert err.value.code == "rate_limited"
    assert err.value.status == 429
    assert err.value.retry_after_s >= 1
    send(c, state, post(**{"from": "other", "nonce": "other"}))
    send(c, state, post(nonce="roll", ts=NOW + 60), now=NOW + 60)


def test_duplicate_returns_original_ack_without_append(c):
    state = fleet_state(c)
    first = send(c, state)
    second = send(c, state, post(nonce="nonce-2", text="changed"))
    assert first["dup"] is False
    assert second["dup"] is True
    assert {k: second[k] for k in ("id", "ts", "msg_id")} == {k: first[k] for k in ("id", "ts", "msg_id")}
    assert len(state.msg_ids) == 1
    assert len(state.ring) == 1
    assert set(first) == {"id", "ts", "msg_id", "dup"}


def test_evicted_cursor_never_replays_ring(c):
    ring = [f"1700000000000-{n:06d}" for n in range(1, 1002)]
    evicted = ring.pop(0)
    with pytest.raises(c.BusError) as err:
        c.check_cursor(evicted, ring)
    assert (err.value.code, err.value.status) == ("cursor_gone", 410)
    assert c.check_cursor(ring[-1], ring) == len(ring)


def test_nonce_and_ring_storage_are_bounded(c):
    state = fleet_state(c)
    for n in range(c.NONCE_CAP + 1):
        # Distinct senders avoid the unrelated per-sender rate gate.
        send(c, state, post(**{"from": f"sender-{n}", "nonce": f"n-{n}", "msg_id": f"m-{n}"}))
    assert len(state.seen) <= c.NONCE_CAP
    assert len(state.ring) == c.RING_CAP


@pytest.mark.parametrize("text", ["", "\ud800", "\udfff", "\x00\r\n", "\u2028\u2029", "x" * 10000], ids=["empty", "high-surrogate", "low-surrogate", "controls", "separators", "long"])
def test_scrub_never_raises_on_string(c, text):
    result = c.scrub_text(text)
    assert isinstance(result, str)
    assert len(result) <= c.TEXT_MAX


def test_read_missing_or_bad_signature_never_discloses_text(c):
    # check_read is an explicitly required transport adapter seam: no open GET.
    for sig in (None, "", "0" * 64):
        headers = {"X-Fleet-From": "reader", "X-Fleet-Ts": str(NOW),
                   "X-Fleet-Nonce": "read-1"}
        if sig is not None:
            headers["X-Fleet-Sig"] = sig
        with pytest.raises(c.BusError) as err:
            c.check_read(lambda kid: SECRET, headers, "", NOW, fleet_state(c))
        assert err.value.status == 401
        assert c.error_body(err.value) == {
            "error": {"code": "bad_signature", "message": "Request rejected"}}


def test_validate_post_non_dict_input_is_validation_error(c):
    with pytest.raises(c.BusError) as err:
        c.validate_post(lambda kid: SECRET, "not a wire object", "", NOW, fleet_state(c))
    assert (err.value.code, err.value.status) == ("bad_request", 400)


@pytest.mark.parametrize("limit,expected", [(None, 100), (1, 1), (200, 200)])
def test_read_limit(c, limit, expected):
    assert c.parse_limit(limit) == expected


@pytest.mark.parametrize("limit", [0, 201, -1, True, "bad"])
def test_bad_read_limit(c, limit):
    with pytest.raises(c.BusError) as err:
        c.parse_limit(limit)
    assert (err.value.code, err.value.status) == ("bad_request", 400)


def test_read_response_and_delivery_cursor(c):
    state = fleet_state(c)
    envelope = post(trace_id="trace", key="group", run_id="run", attempt_n=1)
    ack = send(c, state, envelope)
    # read_records is the response-shaping seam; authentication is check_read.
    response = c.read_records(state, cursor="", limit=100)
    assert set(response) == {"cursor", "records"}
    assert response["cursor"] == ack["id"]
    record, = response["records"]
    assert record == dict({k: envelope[k] for k in
                          ("from", "to", "msg_id", "text", "trace_id", "key", "run_id", "attempt_n")},
                         id=ack["id"], ts=ack["ts"])
    assert c.read_records(state, cursor=response["cursor"], limit=100) == {
        "cursor": response["cursor"], "records": []}
