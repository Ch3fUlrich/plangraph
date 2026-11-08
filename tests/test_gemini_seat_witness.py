"""D-4997: Gemini reviews use an authenticated gateway witness, not child metadata."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from fake_pid import FAKE_PID
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.spawner import gateway_witness as gw


LEG = next(leg for leg in DEFAULT_LEGS if leg.id == 'omniroute-agy-gemini-3.8-flash')


@pytest.mark.parametrize('listing', [None, frozenset()])
def test_gateway_gemini_host_admission_without_listing(monkeypatch, listing):
    from plangraph.routing_support import resolver
    from plangraph.routing_support.opencode_render import render_gateway_providers

    env = {'OMNIROUTE_BASE_URL': 'http://gateway.test/v1',
           'OPENCODE_CONFIG_CONTENT': '{}'}
    monkeypatch.setattr(resolver, 'host_probe_env', lambda extra=None: dict(env))
    monkeypatch.setattr(resolver, 'served_models', lambda *a, **kw: listing)
    assert resolver.host_skip_reason({'model': LEG.model, 'harness': 'opencode'},
                                     probe=lambda child: listing) is None
    unknown = 'omniroute/agy/unknown-model'
    assert resolver.host_skip_reason({'model': unknown, 'harness': 'opencode'},
                                     probe=lambda child: frozenset()) == 'not_served_on_host: ' + unknown
    assert not resolver.admitted_by_render(unknown)
    assert render_gateway_providers(env, LEG.model) is None
    provider = json.loads(env['OPENCODE_CONFIG_CONTENT'])['providers']['omniroute']
    assert provider['models']['agy/gemini-3.8-flash-high']['modelID'] == 'agy/gemini-3.8-flash-high'
    assert provider['settings']['baseURL'] == env['OMNIROUTE_BASE_URL']


def test_review_selects_gateway_gemini_at_agy_position():
    from plangraph.catalog_data import SEED_MODELS
    row = next(row for row in SEED_MODELS if row['model_id'] == LEG.model)
    assert row['available'] == 1 and row['family'] == LEG.family
    ids = [leg.id for leg in DEFAULT_LEGS]
    assert ids.index(LEG.id) + 1 == ids.index('agy-gemini-3.8-flash')
    assert (LEG.family, LEG.harness, LEG.roles, LEG.tool_mode) == (
        'gemini', 'opencode', ('review',), 'tools')
    plan = plan_spawn({'estimate_tokens': 20000}, role='review',
                      writer_families=('gpt',), benchmark={},
                      exclude_families=('qwen', 'deepseek', 'glm', 'mimo', 'grok', 'muse', 'claude'),
                      leg_reader=lambda *a, **kw: {'state': 'available', 'evidence': None})
    assert plan.first_leg == LEG
    assert LEG in plan.review_seats


@pytest.mark.usefixtures('fake_ownership')
def test_runner_records_gemini_gateway_served_model(tmp_path, monkeypatch):
    from plangraph.spawner import resolver
    from plangraph.spawner.runner import SpawnerRunner

    async def probe(*args, **kwargs):
        return None

    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    seen = {}

    def fetch(tag, env):
        cfg = seen['cfg']
        assert gw.tagged(json.dumps(cfg), seen['job_id'], 'omniroute')
        # Recorded call-log schema used by test_gateway_witness, scoped to this session.
        return [{'sessionTag': tag, 'status': 200,
                 'model': 'gemini-3.8-flash', 'provider': 'agy'}]

    monkeypatch.setattr(gw, 'fetch_rows', fetch)

    async def factory(*args, **kwargs):
        seen['cfg'] = json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
        providers = seen['cfg'].get('providers') or seen['cfg'].get('provider')
        tag = providers['omniroute']['options']['headers'][gw.SESSION_HEADER]
        seen['job_id'] = tag.removeprefix('plangraph-')
        assert gw.TOKEN_ENV not in kwargs['env']

        async def communicate():
            return b'VERDICT: ACCEPT', b''

        return SimpleNamespace(pid=FAKE_PID, returncode=0, communicate=communicate)

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('review', model_or_combo=LEG.model, role='review',
                                 client=LEG.harness, cwd=str(tmp_path),
                                 extra_env={'XDG_DATA_HOME': str(tmp_path)}, wait_seconds=3)
        await runner._tasks[job.job_id]
        return job

    job = asyncio.run(run())
    assert job.served_model == 'agy/gemini-3.8-flash'
    assert job.served_family == 'gemini'
    assert job.witness_kind == gw.WITNESS_KIND
    assert job.status == 'completed'
