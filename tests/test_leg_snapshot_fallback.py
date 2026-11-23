"""Snapshot read failures retain the production reader's declared-state fallback."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state, quota_snapshots
from plangraph.routing_support.leg_state_flag import default_reader as production_reader
from plangraph.routing_support.liveness import require_available
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner import leg_probe


@pytest.mark.parametrize(('listed', 'reason'), [
    (False, 'not_listed'),
    (True, 'provider_inactive'),
])
def test_pinned_leg_refusal_preserves_availability_reason(tmp_path, listed, reason):
    model = 'opencode-go/sample'
    provider = SimpleNamespace(snapshot=lambda: {
        'opencode-go': quota_snapshots.ProviderState('opencode-go', False, 'inactive', ''),
    })
    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')
    reader = leg_state.LegAdapter(store, provider, [model] if listed else [], enabled=True)
    leg = SimpleNamespace(id='sample', model=model, harness='opencode')
    with pytest.raises(SpawnRefused, match=f'^leg {reason}: sample$'):
        require_available(leg, reader, {})


def test_default_reader_snapshot_error_fallback_still_probes(tmp_path, monkeypatch, caplog):
    reads, probes = [], []

    def snapshot():
        reads.append('read')
        raise OSError('snapshot unavailable')

    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr(leg_state, '_default_store', lambda: store)
    monkeypatch.setattr(quota_snapshots, 'from_env', lambda: SimpleNamespace(snapshot=snapshot))
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models', staticmethod(lambda: []))
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    production_reader.cache_clear()
    try:
        reader = production_reader()
        leg = SimpleNamespace(id='sample', model='opencode-go/sample', harness='opencode')
        require_available(leg, reader, {})
        assert reader(leg.model) == dict(state='ok', evidence=None,
            reason='leg_state_snapshot_error: OSError: snapshot unavailable')

        async def probe(*args, **kwargs):
            assert kwargs['store'] is store
            probes.append('probe')
            return dict(ok=True)

        monkeypatch.setattr(leg_probe, 'probe', probe)
        assert asyncio.run(leg_probe.before_launch(
            SimpleNamespace(leg_reader=reader), leg, 'review', False)) is None
        assert reads == ['read', 'read'] and probes == ['probe']
        assert 'leg_state_snapshot_error: OSError: snapshot unavailable' in caplog.text
    finally:
        production_reader.cache_clear()


def test_snapshot_recovery_log_redacts_error_values(tmp_path, monkeypatch, caplog):
    secret = 'snapshot-credential'
    monkeypatch.setenv('SNAPSHOT_TEST_TOKEN', secret)
    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')

    def snapshot():
        raise OSError('snapshot unavailable: ' + secret + ' Bearer sk-example')

    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=snapshot),
                                  models=[], enabled=True)
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')

    async def probe(*args, **kwargs):
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(
        SimpleNamespace(leg_reader=reader), leg, 'review', False)) is None
    assert 'leg_state_snapshot_error: OSError: snapshot unavailable' in caplog.text
    assert secret not in caplog.text and 'sk-example' not in caplog.text
    assert '[autoos:redacted]' in caplog.text and '[REDACTED]' in caplog.text


def test_service_route_records_snapshot_error_and_uses_declared_state(monkeypatch):
    from plangraph.routing_support.leg_state_flag import service_plan
    from plangraph.planner.legs import DEFAULT_LEGS

    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'cx-gpt-6.2-sol-medium')
    reason = 'leg_state_snapshot_error: OSError: snapshot unavailable'
    store = SimpleNamespace(leg_status=lambda *a, **kw: dict(state='ok', evidence=None))
    reader = SimpleNamespace(enabled=True, store=store,
        leg_status=lambda *a, **kw: dict(state='unknown', reason=reason, evidence=None))
    service = SimpleNamespace(leg_reader=reader, hung=SimpleNamespace(rank=lambda ids: ids))
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    plan = service_plan(service, card={'estimate_tokens': 100}, role='implement', legs=(leg,))
    assert plan.legs == (leg,)
    assert plan.candidates[0]['leg_state_reason'] == reason


@pytest.mark.parametrize('codex_installed,evidence', [
    (False, None), (True, None), (True, '429'), (True, 'expired'),
    (True, 'model_unavailable'), (True, 'provider_busy'), (True, 'auth_refused'),
])
def test_service_route_requires_real_codex_quota_for_claude(tmp_path, monkeypatch,
                                                           codex_installed, evidence):
    from plangraph.planner.legs import DEFAULT_LEGS, LEAD_WRITER
    from plangraph.routing_support import leg_state_flag
    from plangraph.spawner.service import SpawnService

    models = {leg.id: leg.model for leg in DEFAULT_LEGS}
    fallback = 'agy-claude-sonnet-5-5-high'
    inactive = 'opencode-go-glm-5.3-flash'
    unreachable = 'opencode-deepseek-v4.1-flash-free'
    unlisted = 'opencode-go-deepseek-v4-flash'
    # An advertised model with no corresponding provider snapshot is unreachable.
    states = {
        'opencode-go': quota_snapshots.ProviderState('opencode-go', False, 'inactive', ''),
    }
    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')
    now = time.time()
    store.record_provider_response('agy:gemini-4-hydrogen', 429, 'quota', {'retry-after': '300'},
                                   'fixture', now=now)
    if evidence in ('429', 'expired'):
        store.record_provider_response(models[LEAD_WRITER], 429, 'quota', {'retry-after': '300'},
                                       'fixture', now=now if evidence == '429' else now - 301)
    elif evidence:
        line = {
            'model_unavailable': 'model_unavailable: Error: Model unavailable',
            'provider_busy': 'HTTP 503: upstream unavailable',
            'auth_refused': 'Error: authentication failed',
        }[evidence]
        store.record_harness_limit(models[LEAD_WRITER], line, 'spawner:harness', now=now)
    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=lambda: states),
                                 [models[inactive], models[unreachable]], enabled=True)
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    monkeypatch.setattr(leg_state_flag, 'default_reader', lambda: reader)
    service = SpawnService(SimpleNamespace(), state=tmp_path / 'state')
    service.client_probe = lambda name: codex_installed or name != 'codex'
    plan = service.route({'estimate_tokens': 1000}, repository_preview=True).to_dict()
    skips = {row['leg']: row['reason'] for row in plan['skipped']}
    assert skips[inactive] == 'provider_inactive'
    assert skips[unreachable] == 'unreachable'
    assert skips[unlisted] == 'not_listed'
    if evidence == '429':
        assert skips[LEAD_WRITER] == 'limited'
        assert fallback in {leg['id'] for leg in plan['legs']}
    elif evidence not in (None, 'expired'):
        assert skips[LEAD_WRITER] == evidence
    elif codex_installed:
        assert plan['first_leg']['id'] == LEAD_WRITER
        assert plan['first_leg']['harness'] == 'codex'
    else:
        assert skips[LEAD_WRITER] == 'not_listed'
    if evidence != '429':
        assert skips[fallback] == 'claude_requires_gpt_and_hydrogen_limits'


def test_provider_snapshot_limit_requires_a_current_reset(tmp_path):
    reset = '1970-01-01T00:02:00+00:00'
    limited = quota_snapshots.ProviderState('opencode-go', False, 'rate_limited', reset)
    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')
    model = 'opencode-go/sample'
    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=lambda: {'opencode-go': limited}),
                                 [model], enabled=True)
    assert reader(model, now=119) == dict(state='limited', reason='rate_limited',
                                         evidence={'reset_at': 120})
    assert reader(model, now=120) == dict(state='down', reason='provider_inactive', evidence=None)


@pytest.mark.parametrize('harness,booked', [('codex', False), ('opencode', False), ('codex', True)])
def test_launch_admission_uses_the_selected_harness(tmp_path, monkeypatch, harness, booked):
    model = 'omniroute/cx-gpt-6.2-sol-medium'
    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')
    if booked:
        store.record_provider_response(model, 429, 'quota', {'retry-after': '300'}, 'fixture')
    snapshot = {'omniroute': quota_snapshots.ProviderState('omniroute', True, '', '')}
    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=lambda: snapshot), [], enabled=True)
    leg = SimpleNamespace(model=model, harness=harness)
    probes = []

    async def probe(runner, chosen, **kwargs):
        probes.append(chosen.harness)
        return dict(ok=True)

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', probe)
    skip = asyncio.run(leg_probe.before_launch(
        SimpleNamespace(leg_reader=reader), leg, 'implement', False))
    if booked:
        assert skip.startswith('limited until ')
        assert probes == []
    elif harness == 'codex':
        assert skip is None
        assert probes == ['codex']
    else:
        assert skip == 'not_listed'
        assert probes == []
