"""The live default route (SpawnService.route) fences legacy and argon legs."""
import pytest

from plangraph.planner import legs
from plangraph.routing_support import resolver
from plangraph.spawner.service import SpawnService

ROLES = ('implement', 'review', 'orchestrate')
CARD = {'estimate_tokens': 1000}
DIFF = dict(CARD, diff_ref={'repo': '/repo', 'base': 'HEAD~1', 'head': 'HEAD'})


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: ())
    return SpawnService(state=tmp_path, leg_reader=lambda *a, **kw: {'state': 'ok', 'evidence': None})


def routed(plan):
    return [leg.id + leg.model for leg in (*plan.legs, *plan.review_seats)]


@pytest.mark.parametrize('role', ROLES)
def test_live_default_route_never_routes_legacy(service, role):
    plan = service.route(DIFF, role, writer_families=('claude',))
    assert plan.legs
    assert not [i for i in routed(plan) if 'gemini-3.8-flash' in i.lower() or 'gpt-6-luna' in i.lower()]


@pytest.mark.parametrize('card', [CARD, DIFF], ids=['no_diff', 'diff'])
@pytest.mark.parametrize('family', ['anthropic', 'claude'])
def test_live_default_review_route_never_uses_argon(service, card, family):
    plan = service.route(card, 'review', writer_families=(family,))
    assert plan.legs and plan.review_seats
    assert not [i for i in routed(plan) if 'argon' in i.lower()]


def test_live_default_legs_keep_pinnable_legs_in_registry():
    ids = {leg.id for leg in legs.DEFAULT_LEGS}
    assert {'agy-gemini-3.8-flash', 'cx-gpt-6-luna', 'agy-gemini-4-argon'} <= ids
