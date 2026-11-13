import json
import pytest

from plangraph.spawner import cli
from plangraph.routing_support import resolver


def test_catalog_is_read_only_projection(monkeypatch, capsys):
    rows = [dict(id='registered', model='provider/model', roles=['review'], secret='omit')]
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: rows)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: (_ for _ in ()).throw(AssertionError('service invoked')))
    assert cli.main(['catalog']) == 0
    assert json.loads(capsys.readouterr().out) == [
        dict(id='registered', model='provider/model', roles=['review'])]
    assert rows[0]['secret'] == 'omit'


def test_catalog_missing_role_metadata_is_not_inferred(monkeypatch, capsys):
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [dict(id='catalog-only')])
    assert cli.main(['catalog']) == 0
    assert json.loads(capsys.readouterr().out) == [dict(id='catalog-only', model='catalog-only', roles=[])]


@pytest.mark.parametrize('lid,model', [
    ('omniroute-deepseek-flash', 'omniroute/deepseek-direct-flash'),
    ('omniroute-muse-spark', 'omniroute/muse-spark-1.3-contributor'),
    ('agy-sonnet-5.5', 'claude-sonnet-5-5-medium'),
    ('agy-opus-5.5', 'claude-opus-5-5-high'),
    ('agy-gemini-4-argon', 'gemini-4-argon-high'),
    ('opencode-big-pickle', 'opencode/big-pickle'),
    ('opencode-deepseek-v4.1-flash-free', 'opencode/deepseek-v4.1-flash-free'),
    ('opencode-go-gemini-4-argon', 'opencode-go/gemini-4-argon'),
])
def test_missing_legs_registered_for_utility(lid, model):
    from plangraph.planner.legs import DEFAULT_LEGS, leg_active
    from plangraph.spawner.resolver import DEFAULT_FALLBACK_MODELS
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == lid)
    expected_roles = {'review', 'implement'} | ({'orchestrate'} if lid in {
        'agy-sonnet-5.5', 'omniroute-deepseek-flash', 'omniroute-muse-spark'} else set())
    if lid in {'agy-sonnet-5.5', 'agy-opus-5.5'}:
        expected_roles.remove('implement')
    if lid == 'agy-gemini-4-argon':
        expected_roles.remove('review')
    if lid == 'opencode-deepseek-v4.1-flash-free':
        expected_roles.remove('implement')
    if lid == 'opencode-go-gemini-4-argon':
        expected_roles = set()
    active = lid != 'opencode-go-gemini-4-argon'
    assert (leg.model, leg_active(leg), set(leg.roles)) == (model, active, expected_roles)
    if not active:
        assert leg.disabled == 'D-5648: no review role; retained registration (D-3828)'
    row = next(row for row in DEFAULT_FALLBACK_MODELS if row['id'] == model)
    if active:
        assert (row['active'], row['family'], row['harness']) == (True, leg.family, leg.harness)
    else:
        assert row['id'] == leg.model and row['family'] == leg.family
        assert not leg_active(leg) and not leg.roles  # catalog visibility cannot enable a review seat
