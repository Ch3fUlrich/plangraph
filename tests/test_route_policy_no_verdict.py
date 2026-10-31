"""LE-SEAT-NO-VERDICT: route policy never seats a no-verdict model for review."""
from plangraph.planner.legs import Leg
from plangraph.planner.route_policy import RoutePolicy


def test_skip_reason_names_no_verdict_seat_for_review_only():
    policy = RoutePolicy()
    for model in ('mimo/mimo-v2.6-flash', 'mimo/mimo-v2.6-pro', 'opencode-go/qwen3.8-flash'):
        seat = Leg('nv', 'subscription', 'opencode', model, 'mimo', ('128k',), 200000, False)
        assert policy.skip_reason(seat, 'review') == 'no_verdict_seat'
        assert policy.skip_reason(seat, 'implement') is None
