"""Additional stateful boundary coverage for the existing bus contract."""

import json

import pytest

from plangraph.fleet_bus import contract as c

SECRET = b"test-secret-not-real"
NOW = 1700000000


def send(state, *, now=NOW, body_size=None, **overrides):
    envelope = {"from": "sender", "to": "receiver", "ts": now,
                "nonce": "nonce-1", "msg_id": "message-1", "text": "hello"}
    envelope.update(overrides)
    body = json.dumps(envelope).encode("utf-8")
    if body_size is not None:
        assert len(body) <= body_size
        body += b" " * (body_size - len(body))
        assert len(body) == body_size
    return c.validate_post(lambda kid: SECRET, body,
                           c.sign(SECRET, c.canonical_post(envelope)), now, state)


def read_headers(nonce="read-1", cursor=""):
    return {"X-Fleet-From": "reader", "X-Fleet-Ts": str(NOW),
            "X-Fleet-Nonce": nonce,
            "X-Fleet-Sig": c.sign(SECRET, c.canonical_read(NOW, nonce, "reader", cursor=cursor))}


def test_authenticated_read_then_page():
    state = c.new_state()
    first = send(state)
    second = send(state, nonce="nonce-2", msg_id="message-2", text="second")
    cursor = first["id"]
    assert c.check_read(lambda kid: SECRET, read_headers(cursor=cursor),
                        cursor, NOW, state) == "reader"
    page = c.read_records(state, cursor=cursor, limit=1)
    assert page == {"cursor": second["id"], "records": [
        {"from": "sender", "to": "receiver", "msg_id": "message-2",
         "text": "second", "id": second["id"], "ts": NOW}]}


def test_post_nonce_cannot_be_reused_for_read():
    state = c.new_state()
    send(state)
    with pytest.raises(c.BusError) as err:
        c.check_read(lambda kid: SECRET, read_headers(nonce="nonce-1"), "", NOW, state)
    assert (err.value.code, err.value.status) == ("replayed_nonce", 401)


def test_clock_rollback_keeps_ids_increasing():
    state = c.new_state()
    first = send(state)
    second = send(state, now=NOW - 1, nonce="nonce-2", msg_id="message-2")
    assert first["id"] == f"{NOW * 1000}-000001"
    assert second["id"] == f"{NOW * 1000}-000002"
    assert second["id"] > first["id"]


def test_sequence_overflow_advances_logical_millisecond():
    state = c.new_state()
    # Seed the boundary rather than sending a million rate-limited requests.
    state.last_ms = NOW * 1000
    state.sequence = 999998
    last = send(state)
    overflow = send(state, nonce="nonce-2", msg_id="message-2")
    assert last["id"] == f"{NOW * 1000}-999999"
    assert overflow["id"] == f"{NOW * 1000 + 1}-000001"


@pytest.mark.parametrize("ahead", [60, 61])
def test_server_behind_skew_boundary(ahead):
    state = c.new_state()
    if ahead == 60:
        assert send(state, ts=NOW + ahead)["dup"] is False
    else:
        with pytest.raises(c.BusError) as err:
            send(state, ts=NOW + ahead)
        assert (err.value.code, err.value.status) == ("skew", 401)
        assert not state.seen


def test_exact_body_max_is_accepted():
    state = c.new_state()
    assert send(state, body_size=c.BODY_MAX)["dup"] is False
    assert len(state.ring) == 1


def test_read_records_rejects_cursor_evicted_by_ring_overflow():
    state = c.new_state()
    first = send(state)
    for n in range(1, c.RING_CAP + 1):
        # Independent senders avoid the unrelated per-sender rate limit.
        send(state, **{"from": f"sender-{n}", "nonce": f"nonce-{n + 1}",
                       "msg_id": f"message-{n + 1}"})
    assert len(state.ring) == c.RING_CAP
    assert first["id"] not in {record["id"] for record in state.ring}
    with pytest.raises(c.BusError) as err:
        c.read_records(state, cursor=first["id"])
    assert (err.value.code, err.value.status) == ("cursor_gone", 410)


def reject_key_lookup(kid):
    pytest.fail("Shape errors must precede key resolution")


@pytest.mark.parametrize("cursor", ["\ud800", "\udfff", "prefix\ud800suffix"])
@pytest.mark.parametrize("signature_present", [True, False])
def test_surrogate_read_cursor_shape_and_signature_precedence(cursor, signature_present):
    state = c.new_state()
    headers = read_headers()
    if not signature_present:
        headers.pop("X-Fleet-Sig")
    with pytest.raises(c.BusError) as err:
        c.check_read(reject_key_lookup, headers, cursor, NOW, state)
    expected = ("bad_request", 400) if signature_present else ("bad_signature", 401)
    assert (err.value.code, err.value.status) == expected
    assert not state.seen
    assert not state.limiter.senders


@pytest.mark.parametrize("char", ["\ud800", "\udfff", "\u00a0", "\u2028", "\u2029", "\u3000"])
@pytest.mark.parametrize("field", ["from", "to", "nonce", "msg_id", "trace_id", "key", "run_id", "kid"])
def test_post_identifier_unicode_shape_rejected_before_auth(field, char):
    state = c.new_state()
    envelope = {"from": "sender", "to": "receiver", "ts": NOW,
                "nonce": "nonce-1", "msg_id": "message-1", "text": "hello"}
    envelope[field] = "a" + char + "b"
    with pytest.raises(c.BusError) as err:
        c.validate_post(reject_key_lookup, json.dumps(envelope).encode("utf-8"),
                        "invalid", NOW, state)
    assert (err.value.code, err.value.status) == ("bad_request", 400)
    assert not state.seen


@pytest.mark.parametrize("char", ["\ud800", "\udfff", "\u00a0", "\u2028", "\u2029", "\u3000"])
@pytest.mark.parametrize("header", ["X-Fleet-From", "X-Fleet-Nonce", "X-Fleet-Kid"])
def test_read_identifier_unicode_shape_rejected_before_auth(header, char):
    state = c.new_state()
    headers = read_headers()
    headers[header] = "a" + char + "b"
    with pytest.raises(c.BusError) as err:
        c.check_read(reject_key_lookup, headers, "", NOW, state)
    assert (err.value.code, err.value.status) == ("bad_request", 400)
    assert not state.seen


@pytest.mark.parametrize("text", ["\ud800", "hello\udfff"])
def test_surrogate_post_text_rejected_before_auth(text):
    envelope = {"from": "sender", "to": "receiver", "ts": NOW,
                "nonce": "nonce-1", "msg_id": "message-1", "text": text}
    with pytest.raises(c.BusError) as err:
        c.validate_post(reject_key_lookup, json.dumps(envelope).encode("utf-8"),
                        "invalid", NOW, c.new_state())
    assert (err.value.code, err.value.status) == ("bad_request", 400)
