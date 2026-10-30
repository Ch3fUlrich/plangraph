"""Pins that run WITH console credit legs present (D-3258/D-3259/D-3264, slice G4).

Each pin runs under an unexpired credit policy and a host listing stubbed to
contain the console-anthropic models, so the credit legs are actually routed:
under the neutral conftest policy they are skipped as ``credit_expired``, and
without positive host evidence they are ``not_served_on_host`` on Linux.
"""
from datetime import date

from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import next_leg_after_provider_stop, plan_spawn
from plangraph.planner.seats import seat_family
from plangraph.routing_support.resolver import plan_for_launch
from plangraph.spawner.leg_state import LegStateStore

CARD = {"estimate_tokens": 1000}
CONSOLE_SONNET = "console-sonnet-5.5"
CONSOLE_SONNET_MODEL = "console-anthropic/claude-sonnet-5-5"
#: Host listing stubbed to contain the console-anthropic models (positive host evidence).
HOST_LISTING = frozenset(leg.model for leg in DEFAULT_LEGS)
POLICY = RoutePolicy(credit_tiers={"console-anthropic": {"expires": "2026-11-30"}},
                     today=date(2026, 11, 1))


def _ok(model, **kwargs):
    return {"state": "ok", "evidence": {}}


def _limited_console_sonnet(model, **kwargs):
    if model == CONSOLE_SONNET_MODEL:
        return {"state": "limited", "evidence": {"status": 429}}
    return _ok(model, **kwargs)


def _host_listing(env):
    return HOST_LISTING


def _writer_entry_after(leg_id, steps=1):
    """``steps`` WRITER_ORDER entries after ``leg_id``, computed (never a literal id)."""
    return WRITER_ORDER[WRITER_ORDER.index(leg_id) + steps]


def test_limited_console_leg_falls_through_to_next_writer_entry():
    """(a) A limited console-sonnet-5.5 yields the next WRITER_ORDER entry first."""
    plan = plan_spawn(CARD, role="implement", leg_reader=_limited_console_sonnet, policy=POLICY)
    assert plan.first_leg.id == _writer_entry_after(CONSOLE_SONNET)
    assert CONSOLE_SONNET not in {leg.id for leg in plan.legs}
    assert any(skip["leg"] == CONSOLE_SONNET and skip["reason"] == "limited"
               for skip in plan.skipped)


def test_provider_stop_on_console_leg_returns_next_non_limited_leg(tmp_path):
    """(b) A provider stop on console-sonnet-5.5 advances to the next non-limited leg."""
    store = LegStateStore(tmp_path / "state.sqlite")
    plan = plan_spawn(CARD, role="implement", leg_reader=store, policy=POLICY, now=100)
    launched = plan_for_launch(plan, probe=_host_listing)
    assert launched.first_leg.id == CONSOLE_SONNET
    # The entry right after console-sonnet-5.5 is limited too (its own 429):
    # the walk must skip it and return the next non-limited leg.
    assert plan.legs[1].id == _writer_entry_after(CONSOLE_SONNET)
    store.record_provider_response(plan.legs[1].model, 429, "quota exhausted",
                                   {"Retry-After": "60"}, "test", now=100)
    nxt = next_leg_after_provider_stop(launched, CONSOLE_SONNET, 429, "quota exhausted",
                                       {"Retry-After": "60"}, "test", store=store, now=100)
    assert nxt.id == _writer_entry_after(CONSOLE_SONNET, steps=2)
    assert nxt.id != plan.legs[1].id


def test_review_seats_for_gpt_writer_include_console_leg_without_family_doubles():
    """(c) A gpt writer's seats include a console leg and never two legs of one family."""
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",), leg_reader=_ok, policy=POLICY)
    launched = plan_for_launch(plan, probe=_host_listing)
    seats = launched.review_seats
    console_seat = next((leg for leg in seats if leg.id.startswith("console-")), None)
    assert console_seat is not None and console_seat in launched.legs
    assert len(seats) >= 2
    families = [seat_family(leg.family) for leg in seats]
    assert len(families) == len(set(families))


def test_claude_family_writer_gets_no_claude_seat():
    """(d) A claude-family writer's review seats never include a claude leg."""
    plan = plan_spawn(CARD, role="review", writer_families=("claude",), leg_reader=_ok, policy=POLICY)
    seats = plan.review_seats
    assert len(seats) >= 2
    assert all(seat_family(leg.family) != seat_family("claude") for leg in seats)
