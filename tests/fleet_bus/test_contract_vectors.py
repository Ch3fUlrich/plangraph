"""JSON cases are executable by Python and a future non-Python runner.

Runner-only inputs (wire padding, preloaded replay/rate/dedupe state) are
explicit JSON, rather than Python expressions or serialized Python objects.
"""
import importlib
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from .test_contract_unit import fleet_state, post, send, SECRET, NOW


CASES = [
    pytest.param(case, id=f"{path.stem}:{case['name']}")
    for path in sorted((Path(__file__).parent / "vectors").glob("*.json"))
    for case in json.loads(path.read_text(encoding="utf-8"))
]


@pytest.mark.parametrize("case", CASES)
def test_vector(case):
    c = importlib.import_module("plangraph.fleet_bus.contract")
    fn, inputs = case["fn"], case.get("inputs", {})

    def invoke():
        if fn == "replay_window":
            state = fleet_state(c)
            for n in range(c.NONCE_CAP):
                send(c, state, post(**{"from": f"s-{n // 30}", "nonce": f"n-{n}", "msg_id": str(n), "ts": NOW + 10}))
            seen = state.seen.copy()
            rates = {k: list(v) for k, v in state.limiter.senders.items()}
            now = inputs.get("now", NOW)
            try:
                send(c, state, post(nonce="fresh", ts=now), now=now)
            except c.BusError:
                assert state.seen == seen
                assert {k: list(v) for k, v in state.limiter.senders.items()} == rates
                assert "n-0" in state.seen
                raise
            return dict(state.seen)
        if fn == "constants":
            return {name: getattr(c, name) for name in case["expect"]}
        if fn == "validate_post":
            state = fleet_state(c)
            envelope = inputs.get("envelope", post())
            if isinstance(envelope, dict):
                envelope = dict(envelope)
            for field, length in inputs.get("repeat_fields", {}).items():
                envelope[field] = "x" * length
            body = inputs.get("raw", json.dumps(envelope, ensure_ascii=False)).encode("utf-8")
            body += b" " * inputs.get("padding", 0)
            if "body_size" in inputs:
                assert len(body) <= inputs["body_size"]
                body += b" " * (inputs["body_size"] - len(body))
            # Invalid shapes still reach validate_post, not canonical_post.
            fields = ["fleet-bus/2", "post", str(inputs.get("normalized_ts", envelope.get("ts", ""))),
                      str(envelope.get("nonce", "")), str(envelope.get("from", "")),
                      str(envelope.get("to", "")), str(envelope.get("msg_id", "")),
                      str(envelope.get("kid", "")),
                      hashlib.sha256(str(envelope.get("text", "")).encode()).hexdigest()] if isinstance(envelope, dict) else []
            sig = hmac.new(SECRET, "|".join(fields).encode(), hashlib.sha256).hexdigest() if inputs.get("signature", "valid") == "valid" else "0" * 64
            if inputs.get("replayed"):
                state.seen[envelope["nonce"]] = NOW
            if inputs.get("rate_full"):
                for n in range(30):
                    prior = post(nonce=f"prior-{n}", msg_id=f"prior-{n}")
                    c.validate_post(lambda kid: SECRET, json.dumps(prior).encode(), c.sign(SECRET, c.canonical_post(prior)), inputs.get("now", NOW), state)
            if inputs.get("duplicate"):
                state.msg_ids[(envelope["from"], envelope["msg_id"])] = {"id": "1700000000000-000001", "ts": NOW, "msg_id": envelope["msg_id"], "dup": False}
            if "retry_after_s" in inputs:
                seen, msg_ids = state.seen.copy(), state.msg_ids.copy()
                with pytest.raises(c.BusError) as rejected:
                    c.validate_post(lambda kid: SECRET, body, sig, inputs.get("now", NOW), state)
                assert (rejected.value.code, rejected.value.status) == ("rate_limited", 429)
                assert state.seen == seen
                assert state.msg_ids == msg_ids
                return c.validate_post(lambda kid: SECRET, body, sig,
                                       inputs.get("now", NOW) + inputs["retry_after_s"], state)
            return c.validate_post(lambda kid: None if inputs.get("unknown_kid") else SECRET, body, sig, inputs.get("now", NOW), state)
        if fn == "check_read":
            state = fleet_state(c)
            headers = dict(inputs["headers"])
            cursor = inputs.get("cursor", "")
            if inputs.get("sign"):
                headers["X-Fleet-Sig"] = c.sign(SECRET, c.canonical_read(
                    headers["X-Fleet-Ts"], headers["X-Fleet-Nonce"], headers["X-Fleet-From"], cursor=cursor))
            if inputs.get("rate_full"):
                for n in range(30):
                    prior = dict(headers, **{"X-Fleet-Nonce": f"prior-{n}"})
                    prior["X-Fleet-Sig"] = c.sign(SECRET, c.canonical_read(
                        prior["X-Fleet-Ts"], prior["X-Fleet-Nonce"], prior["X-Fleet-From"]))
                    c.check_read(lambda kid: SECRET, prior, "", NOW, state)
            try:
                return c.check_read(lambda kid: SECRET, headers, cursor, inputs.get("now", NOW), state, limit=inputs.get("limit", 100))
            except c.BusError:
                if inputs.get("unchanged"):
                    assert not state.seen
                    assert not state.limiter.senders
                    assert not state.msg_ids
                raise
        if fn == "read_records":
            state = fleet_state(c)
            state.ring.extend(inputs.get("records", []))
            return c.read_records(state, inputs.get("cursor", ""), inputs.get("limit", 100))
        if fn == "sign":
            return c.sign(inputs["secret"].encode("utf-8"), inputs["canonical"])
        if fn == "canonical_post":
            return c.canonical_post(inputs["envelope"])
        if fn == "canonical_read":
            return c.canonical_read(**inputs)
        if fn == "check_cursor":
            return c.check_cursor(inputs["cursor"], inputs["ring_ids"])
        if fn == "error_body":
            err = c.BusError(inputs["code"], inputs["message"])
            assert err.status == c.ERROR_STATUS[err.code]
            assert err.message == inputs["message"]
            return c.error_body(err)
        if fn == "parse_ts" and "special" in inputs:
            return c.parse_ts(float(inputs["special"]))
        return getattr(c, fn)(inputs["value"])

    if "error" in case:
        with pytest.raises(c.BusError) as caught:
            invoke()
        assert caught.value.code == case["error"]["code"]
        assert caught.value.status == case["error"]["status"]
        assert isinstance(caught.value.message, str)
        assert SECRET.decode() not in caught.value.message
        assert "0" * 64 not in caught.value.message
        if caught.value.code == "rate_limited":
            assert caught.value.retry_after_s >= 1
    else:
        result = invoke()
        if case.get("subset"):
            assert {key: result[key] for key in case["expect"]} == case["expect"]
        else:
            assert result == case["expect"]
