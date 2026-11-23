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
    ('omniroute-deepseek-flash', 'omniroute/deepseek-v4.1-flash'),
    ('omniroute-muse-spark', 'omniroute/spark-1.3-contributor'),
    ('agy-sonnet-5.5', 'claude-sonnet-5-5-medium'),
    ('agy-opus-5.5', 'claude-opus-5-5-high'),
    ('agy-gemini-4-neon-medium', 'gemini-4-neon-medium'),
    ('opencode-big-pickle', 'opencode/big-pickle'),
    ('opencode-deepseek-v4.1-flash-free', 'opencode/deepseek-v4.1-flash-free'),
    ('opencode-glm-5.3-flash-free', 'opencode/glm-5.3-flash-free'),
])
def test_missing_legs_registered_for_utility(lid, model):
    from plangraph.planner.legs import DEFAULT_LEGS, leg_active
    from plangraph.spawner.resolver import DEFAULT_FALLBACK_MODELS
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == lid)
    expected_roles = {'review', 'implement'} | ({'orchestrate'} if lid in {
        'agy-sonnet-5.5', 'omniroute-deepseek-flash', 'omniroute-muse-spark'} else set())
    if lid in {'agy-sonnet-5.5', 'agy-opus-5.5', 'agy-gemini-4-neon-medium'}:
        expected_roles.remove('implement')
    if lid == 'agy-opus-5.5':
        expected_roles.add('research')
    active = lid != 'opencode-glm-5.3-flash-free'
    assert (leg.model, leg_active(leg), set(leg.roles)) == (model, active, expected_roles)
    if not active:
        assert leg.disabled.startswith('D-6310:')
    row = next(row for row in DEFAULT_FALLBACK_MODELS if row['id'] == model)
    assert (row['active'], row['family'], row['harness']) == (True, leg.family, leg.harness)


def test_writer_snapshot_scores_real_legs_from_seed_catalog():
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.review_order import catalog_score
    rows = resolver.get_catalog_models()
    scores = {leg.id: catalog_score(leg, rows) for leg in DEFAULT_LEGS if 'implement' in leg.roles}
    assert scores['cx-gpt-6.2-sol-medium'] >= 50.0
    assert scores['cx-gpt-6.2-astra'] >= 50.0
    assert scores['mimo-v2.6-pro'] == 46.0
    assert scores['agy-gemini-4-hydrogen-high'] is None


def test_route_leg_scores_reports_catalog_intelligence_per_routed_leg(monkeypatch, capsys):
    from types import SimpleNamespace
    from plangraph.planner.legs import DEFAULT_LEGS
    legs = [leg for leg in DEFAULT_LEGS if leg.id in ('agy-sonnet-5.5', 'mimo-v2.6-pro')]

    class Routed:
        def __init__(self):
            self.legs = tuple(legs)

        def to_dict(self):
            return {'legs': [{'id': leg.id} for leg in legs]}

    class Service:
        def __init__(self, **kw):
            pass

        def route(self, *args, **kw):
            return Routed()

    monkeypatch.setattr(cli, 'SpawnService', Service)
    monkeypatch.setattr('plangraph.spawner.bench_entry.benchmark_options', lambda *a: {})
    card = json.dumps({'estimate_tokens': 1000})
    assert cli.main(['route', '--role', 'orchestrate', '--json', '--leg-scores', '--card', card]) == 0
    out = json.loads(capsys.readouterr().out)
    # The legs keep the route order; the scores carry the catalog strength.
    assert [leg['id'] for leg in out['legs']] == [leg.id for leg in legs]
    assert out['leg_scores']['mimo-v2.6-pro'] == 46.0
    assert out['leg_scores']['agy-sonnet-5.5'] != out['leg_scores']['mimo-v2.6-pro']


def test_research_route_refusal_preserves_per_leg_skips_in_json(tmp_path, capsys):
    card = json.dumps({'estimate_tokens': 950_000})
    assert cli.main(['--state', str(tmp_path), 'route', '--role', 'research',
                     '--json', '--card', card]) == 2
    output = capsys.readouterr()
    error = json.loads(output.out)['error']
    assert 'skip agy-opus-5.5: max_request_tokens' in error
    assert 'skip opencode-go-glm-5.3-flash: max_request_tokens' in error
    assert output.err.strip() == error
