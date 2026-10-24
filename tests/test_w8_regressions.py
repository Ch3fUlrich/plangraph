import asyncio
import pytest
from plangraph.mimo_policy import check_mimo_path
from plangraph.spawner.private_io import create_private
from plangraph.spawner.runner import SpawnerRunner, JobInfo

@pytest.mark.parametrize('model', ['omniroute-mimo-v2.6-flash', 'mimo_v2.6', 'x/foo-mimo-bar', 'mimo-v2.6-flash', 'X/MIMO-bar', r'omniroute\mimo-v2.6-flash'])
def test_token_refusal(model):
    with pytest.raises(ValueError):
        check_mimo_path(model)

@pytest.mark.parametrize('model', ['foo/mimodev', 'foo/mimosa', 'mimo/mimo-v2.6-flash', 'mimo/mimo-v2.6-pro'])
def test_token_allowed(model):
    assert check_mimo_path(model) == model

def test_existing_private(tmp_path):
    path = tmp_path / 'private'
    path.write_text('keep')
    path.chmod(0o644)
    create_private(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == 'keep'

def test_steer_without_stdin():
    runner = SpawnerRunner()
    runner.jobs['j'] = JobInfo('j', 'dry_run', '', '')
    assert asyncio.run(runner.steer('j', 'hello')) == {'ok': False, 'steered': False, 'error': 'no stdin'}

def test_bench_typed():
    from plangraph.planner import routing
    with pytest.raises(routing.BenchRequired) as caught:
        routing.plan_spawn({'estimate_tokens': 1}, role='implement', strict=True)
    assert caught.value.legs

def test_review_exemption_explicit():
    from plangraph.spawner.risk import evaluate_risk
    assert evaluate_risk(paths=['docs/guide.md'], is_pure_doc=True, bucket='S0').review_exempt is True
    assert evaluate_risk(paths=['auth.py'], is_pure_doc=True, bucket='S0').review_exempt is False

@pytest.mark.parametrize('risk', ['normal', 'high', 'critical'])
def test_zero_review_requires_exemption(risk):
    from plangraph.spawner.resolver import resolve_review_plan
    from plangraph.planner.routing import plan_spawn, SpawnRefused
    with pytest.raises(ValueError):
        resolve_review_plan('mimo/mimo-v2.6-pro', risk=risk, reviewer_count=0)
    with pytest.raises(SpawnRefused):
        plan_spawn({'estimate_tokens': 1, 'risk': risk, 'reviewer_count': 0}, role='review')

def test_dry_run_nonce_not_retained():
    async def run():
        runner = SpawnerRunner()
        await runner.spawn('test', model_or_combo='mimo/mimo-v2.6-flash', dry_run=True)
        assert runner._nonces == {}
    asyncio.run(run())
