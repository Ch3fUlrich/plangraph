"""Contract 1b: malformed numbers are client errors (400), never a 500.

Regression for a keeper bug found while freezing the contract: a non-numeric
``n`` query value, ``lease_s`` or priority ``n`` raised outside the refusal
path and surfaced as ``500 internal``.
"""
import pytest

from contract_schema import assert_valid
from contract_world import make_plan, show, to_in_review, verdict


@pytest.mark.parametrize("path,body", [
    ("/v1/ready?n=abc", None),
    ("/v1/percent?by=zzz", None),
])
def test_malformed_query_values_are_client_errors(kp, path, body):
    resp = kp.get(path, "reader")
    assert 400 <= resp.status < 500, resp.raw[:200]
    assert_valid(resp.json, "Error")


def test_malformed_numeric_body_fields_are_client_errors_not_500(kp):
    plan = make_plan(kp)
    for verb, who, body in (
            ("claim", "worker1", {"key": plan.key, "lease_s": "soon"}),
            ("set_priority", "router", {"key": plan.key, "n": "high"}),
            ("set_priority", "router", {"key": plan.key, "n": None}),
            ("heartbeat", "worker1", {"key": plan.key, "epoch": "x"})):
        resp = kp.post(verb, who, body)
        assert 400 <= resp.status < 500, (verb, resp.status, resp.raw[:200])
        assert_valid(resp.json, "Error")


@pytest.mark.parametrize("value", ["false", "true", "no", 0, 1, [], {}, None])
def test_verdict_ok_must_be_a_json_boolean(kp, value):
    """A non-boolean ``ok`` used to be coerced with ``bool()``: the string
    ``"false"`` passed the review.  Only ``true`` / ``false`` are accepted."""
    plan = make_plan(kp)
    to_in_review(kp, plan.key)
    resp = kp.post("verdict", "reviewer1", {"key": plan.key, "ok": value, "reason": "x"})
    assert resp.status == 400, resp.raw[:200]
    assert_valid(resp.json, "Error")
    assert show(kp, plan.key)["state"] == "in_review"
    assert verdict(kp, plan.key).status == 200            # a real boolean still works
