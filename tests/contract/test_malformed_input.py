"""Contract 1b: malformed numbers are client errors (400), never a 500.

Regression for a keeper bug found while freezing the contract: a non-numeric
``n`` query value, ``lease_s`` or priority ``n`` raised outside the refusal
path and surfaced as ``500 internal``.
"""
import pytest

from contract_world import claim, err, make_plan, show, to_in_review, verdict


@pytest.mark.parametrize("path,body", [
    ("/v1/ready?n=abc", None),
    ("/v1/percent?by=zzz", None),
])
def test_malformed_query_values_are_client_errors(kp, path, body):
    err(kp.get(path, "reader"), 400, "validation")


def test_malformed_numeric_body_fields_are_client_errors_not_500(kp):
    plan = make_plan(kp)
    claimed = make_plan(kp)
    claim(kp, claimed.key)                      # heartbeat needs a claimed item to reach epoch parsing
    for verb, who, body in (
            ("claim", "worker1", {"key": plan.key, "lease_s": "soon"}),
            ("set_priority", "router", {"key": plan.key, "n": "high"}),
            ("set_priority", "router", {"key": plan.key, "n": None}),
            ("heartbeat", "worker1", {"key": claimed.key, "epoch": "x"})):
        err(kp.post(verb, who, body), 400, "validation")


@pytest.mark.parametrize("value", ["false", "true", "no", 0, 1, [], {}, None])
def test_verdict_ok_must_be_a_json_boolean(kp, value):
    """A non-boolean ``ok`` used to be coerced with ``bool()``: the string
    ``"false"`` passed the review.  Only ``true`` / ``false`` are accepted."""
    plan = make_plan(kp)
    to_in_review(kp, plan.key)
    err(kp.post("verdict", "reviewer1", {"key": plan.key, "ok": value, "reason": "x"}),
        400, "validation")
    assert show(kp, plan.key)["state"] == "in_review"
    assert verdict(kp, plan.key).status == 200            # a real boolean still works
