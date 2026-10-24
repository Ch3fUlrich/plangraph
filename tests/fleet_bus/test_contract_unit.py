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


def test_rate_rejected_flood_preserves_replay_state(c):
    state = fleet_state(c)
    for n in range(c.RATE_PER_MIN):
        send(c, state, post(nonce=f"accepted-{n}", msg_id=f"accepted-{n}"))
    seen = state.seen.copy()
    msg_ids = state.msg_ids.copy()
    for n in range(c.NONCE_CAP + 1):
        with pytest.raises(c.BusError) as err:
            send(c, state, post(nonce=f"rejected-{n}", msg_id=f"rejected-{n}"))
        assert (err.value.code, err.value.status) == ("rate_limited", 429)
    # Replay must win over rate limiting even after more than a window of rejects.
    with pytest.raises(c.BusError) as err:
        send(c, state, post(nonce="accepted-0", msg_id="accepted-0"))
    assert err.value.code == "replayed_nonce"
    assert state.seen == seen
    assert state.msg_ids == msg_ids
    assert not any(nonce.startswith("rejected-") for nonce in state.seen)


def test_exact_retry_at_inclusive_skew_and_rate_boundary(c):
    state = fleet_state(c)
    for n in range(c.RATE_PER_MIN):
        send(c, state, post(nonce=f"prior-{n}", msg_id=f"prior-{n}"))
    envelope = post(nonce="retry", msg_id="retry")
    with pytest.raises(c.BusError) as err:
        send(c, state, envelope)
    assert err.value.code == "rate_limited"
    assert "retry" not in state.seen
    assert ("sender", "retry") not in state.msg_ids
    ack = send(c, state, envelope, now=NOW + 60)
    assert ack["dup"] is False


def test_rate_rejected_request_can_retry_exactly_at_window_boundary(c):
    # Preserve the original regression entry point; the precise boundary is above.
    test_exact_retry_at_inclusive_skew_and_rate_boundary(c)


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
    for n in range(c.NONCE_CAP):
        # Distinct senders avoid the unrelated per-sender rate gate.
        send(c, state, post(**{"from": f"sender-{n}", "kid": f"k-{n // (c.NONCE_CAP // 32)}", "nonce": f"n-{n}", "msg_id": f"m-{n}"}))
    assert len(state.seen) <= c.NONCE_CAP
    assert len(state.ring) == c.RING_CAP
    with pytest.raises(c.BusError) as err:
        send(c, state, post(**{"from": "overflow", "nonce": "overflow"}))
    assert (err.value.code, err.value.status) == ("replay_window_full", 503)
    assert len(state.seen) == c.NONCE_CAP
    assert len(state.ring) == c.RING_CAP


@pytest.mark.parametrize("cursor,limit,code", [("", 0, "bad_request"), ("1-000001", 100, "cursor_gone")])
def test_read_refusal_preserves_nonce_and_rate(c, cursor, limit, code):
    state = fleet_state(c)
    send(c, state)
    seen = state.seen.copy()
    rates = {key: list(value) for key, value in state.limiter.senders.items()}
    headers = {"X-Fleet-From": "reader", "X-Fleet-Ts": str(NOW), "X-Fleet-Nonce": "read"}
    headers["X-Fleet-Sig"] = c.sign(SECRET, c.canonical_read(NOW, "read", "reader", cursor=cursor))
    with pytest.raises(c.BusError) as err:
        c.check_read(lambda kid: SECRET, headers, cursor, NOW, state, limit=limit)
    assert err.value.code == code
    assert state.seen == seen
    assert {key: list(value) for key, value in state.limiter.senders.items()} == rates


def test_read_auth_before_stale_cursor(c):
    state = fleet_state(c)
    headers = {"X-Fleet-From": "reader", "X-Fleet-Ts": str(NOW),
               "X-Fleet-Nonce": "read", "X-Fleet-Sig": "0" * 64}
    with pytest.raises(c.BusError) as err:
        c.check_read(lambda kid: SECRET, headers, "1-000001", NOW, state)
    assert (err.value.code, err.value.status) == ("bad_signature", 401)
    assert not state.seen
    assert not state.limiter.senders


def test_accepted_flood_fails_closed_and_purges_by_message_ts(c):
    state = fleet_state(c)
    for n in range(c.NONCE_CAP):
        send(c, state, post(**{"from": f"sender-{n // 30}", "nonce": f"n-{n}",
                              "kid": f"k-{n // (c.NONCE_CAP // 32)}", "msg_id": f"m-{n}", "ts": NOW + 10}))
    seen = state.seen.copy()
    rates = {key: list(value) for key, value in state.limiter.senders.items()}
    with pytest.raises(c.BusError) as err:
        send(c, state, post(**{"from": "new", "nonce": "overflow"}))
    assert (err.value.code, err.value.status) == ("replay_window_full", 503)
    assert state.seen == seen
    assert {key: list(value) for key, value in state.limiter.senders.items()} == rates
    oldest = post(**{"from": "sender-0", "nonce": "n-0", "msg_id": "m-0", "ts": NOW + 10})
    with pytest.raises(c.BusError) as err:
        send(c, state, oldest)
    assert err.value.code == "replayed_nonce"
    with pytest.raises(c.BusError) as err:
        send(c, state, post(nonce="boundary", ts=NOW + 70), now=NOW + 70)
    assert err.value.code == "replay_window_full"
    send(c, state, post(nonce="fresh", ts=NOW + 71), now=NOW + 71)
    assert state.seen == OrderedDict([("fresh", NOW + 71)])
    with pytest.raises(c.BusError) as err:
        send(c, state, oldest, now=NOW + 71)
    assert err.value.code == "skew"


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
    def page(nonce, cursor):
        headers = {"X-Fleet-From": "reader", "X-Fleet-Ts": str(NOW),
                   "X-Fleet-Nonce": nonce}
        headers["X-Fleet-Sig"] = c.sign(SECRET, c.canonical_read(NOW, nonce, "reader", cursor=cursor))
        return c.read_records(state, c.check_read(lambda kid: SECRET, headers, cursor, NOW, state))
    response = page("page-1", "")
    assert set(response) == {"cursor", "records"}
    assert response["cursor"] == ack["id"]
    record, = response["records"]
    assert record == dict({k: envelope[k] for k in
                          ("from", "to", "msg_id", "text", "trace_id", "key", "run_id", "attempt_n")},
                         id=ack["id"], ts=ack["ts"])
    assert page("page-2", response["cursor"]) == {
        "cursor": response["cursor"], "records": []}


def test_global_window_shared_by_keys_and_default_senders(c):
    state = fleet_state(c)
    for n in range(c.NONCE_CAP - 1):
        send(c, state, post(**{"from": f"identity-{n}", "nonce": str(n), "ts": NOW + 10}))
    send(c, state, post(kid="other", nonce="other", ts=NOW + 20))
    before = state.seen.copy()
    with pytest.raises(c.BusError) as err:
        send(c, state, post(nonce="overflow"), now=NOW + 0.5)
    assert (err.value.code, err.value.status, err.value.retry_after_s) == ("replay_window_full", 503, 70)
    assert state.seen == before
    with pytest.raises(c.BusError) as err:
        send(c, state, post(kid="third", nonce="third"))
    assert (err.value.code, err.value.retry_after_s) == ("replay_window_full", 70)
    before = state.seen.copy()
    rates = {key: list(value) for key, value in state.limiter.senders.items()}
    headers = {"X-Fleet-From": "new-reader", "X-Fleet-Ts": str(NOW), "X-Fleet-Nonce": "read-full"}
    headers["X-Fleet-Sig"] = c.sign(SECRET, c.canonical_read(NOW, "read-full", "new-reader"))
    with pytest.raises(c.BusError) as err:
        c.check_read(lambda kid: SECRET, headers, "", NOW, state)
    assert (err.value.code, err.value.retry_after_s) == ("replay_window_full", 70)
    assert state.seen == before
    assert {key: list(value) for key, value in state.limiter.senders.items()} == rates
    send(c, state, post(nonce="after-expiry", ts=NOW + 71), now=NOW + 71)
    assert state.seen == OrderedDict([("other", NOW + 20), ("after-expiry", NOW + 71)])


def test_at_cap_scans_once(c):
    class OneScan(OrderedDict):
        scans = 0

        def items(self):
            self.scans += 1
            assert self.scans == 1
            return super().items()

    state = fleet_state(c)
    state.seen = OneScan((str(n), NOW) for n in range(c.NONCE_CAP))
    with pytest.raises(c.BusError) as err:
        send(c, state)
    assert (err.value.code, err.value.retry_after_s) == ("replay_window_full", 60)
    assert state.seen.scans == 1


def test_no_replay_scan_below_cap(c):
    class NoScan(OrderedDict):
        def items(self):
            raise AssertionError("replay scan below cap")
    state = fleet_state(c)
    state.seen = NoScan()
    send(c, state)
    send(c, state, post(nonce="next"))


def test_read_validated_result_is_only_page_input(c):
    state = fleet_state(c)
    send(c, state)
    headers = {"X-Fleet-From": "reader", "X-Fleet-Ts": str(NOW), "X-Fleet-Nonce": "read"}
    headers["X-Fleet-Sig"] = c.sign(SECRET, c.canonical_read(NOW, "read", "reader"))
    accepted = c.check_read(lambda kid: SECRET, headers, "", NOW, state, limit="1")
    assert accepted == ("reader", 1, 0)
    with patch.object(c, "parse_limit", side_effect=AssertionError("late validation")), patch.object(c, "check_cursor", side_effect=AssertionError("late validation")):
        assert len(c.read_records(state, accepted)["records"]) == 1
