import asyncio
from dataclasses import replace

import pytest

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService


def test_live_skip_precedes_typed_disabled_entries():
    live = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.1-sol-xhigh')
    disabled_writer = replace(live, id='disabled-test-writer', disabled='synthetic disable')
    evidence = {'status': 429}
    plan = plan_spawn({'estimate_tokens': 1000}, role='implement', legs=(live, disabled_writer), leg_reader=lambda model, **kw: {
        'state': 'limited' if model == live.model else 'ok',
        'evidence': evidence if model == live.model else None})
    assert plan.skipped[0]['evidence'] == evidence
    disabled = [s for s in plan.skipped if s['reason'].startswith('disabled:')]
    assert disabled
    assert all(s['evidence']['kind'] == 'leg_disabled' for s in disabled)


def test_disabled_models_still_validated():
    leg = replace(DEFAULT_LEGS[0], model='nonexistent/typo', disabled='test disable')
    with pytest.raises((ValueError, SpawnRefused)):
        plan_spawn({'estimate_tokens': 1000}, role='implement', legs=(leg,))


@pytest.mark.parametrize('leg_id', ['opencode-go-deepseek-v4-flash', 'opencode-go-longcat-2.5-free'])
def test_explicit_client_mismatch_refused(tmp_path, leg_id):
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    with pytest.raises(SpawnRefused, match='client.*harness'):
        asyncio.run(runner.spawn('review', model_or_combo=leg_id,
                                 client='claude', dry_run=True))
    assert not runner.jobs and not runner._tasks


@pytest.mark.parametrize('reason', [None, 'operator override'])
def test_disabled_override_matches_runner(tmp_path, reason):
    leg = next(l for l in DEFAULT_LEGS if l.id == 'opencode-glm-5.3-flash-free')
    service = SpawnService(state=tmp_path)
    with pytest.raises(SpawnRefused) as service_error:
        asyncio.run(service._spawn(card={'estimate_tokens': 1000}, leg=leg.id,
                                   override_reason=reason, dry_run=True))
    with pytest.raises(SpawnRefused) as runner_error:
        asyncio.run(service.runner.spawn('test', model_or_combo=leg.id, dry_run=True))
    assert str(service_error.value) == str(runner_error.value)
    assert 'override cannot enable a disabled leg' in str(service_error.value)
    assert not (tmp_path / 'spawn-overrides.jsonl').exists()
