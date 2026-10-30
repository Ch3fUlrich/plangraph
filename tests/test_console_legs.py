"""Console credit routing contracts (D-3258)."""
from datetime import date

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS, WRITER_ORDER
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import validate_model


def test_console_legs():
    assert TIERS == ('credit', 'trial', 'free', 'subscription', 'paid')
    assert WRITER_ORDER[0] == 'console-sonnet-5.5'
    for name in ('sonnet', 'opus', 'haiku'):
        model = f'console-anthropic/claude-{name}-5-5'
        assert validate_model(model) == model
        leg = next(l for l in DEFAULT_LEGS if l.model == model)
        assert leg.tier == 'credit' and leg.family == 'claude'


def test_credit_expiry():
    leg = next(l for l in DEFAULT_LEGS if l.id == 'console-sonnet-5.5')
    policy = RoutePolicy(credit_tiers={'console-anthropic': {'expires': '2026-11-30'}},
                         today=date(2026, 12, 1))
    assert policy.skip_reason(leg, 'implement') == 'credit_expired'
    assert RoutePolicy().skip_reason(leg, 'implement') == 'credit_expired'
