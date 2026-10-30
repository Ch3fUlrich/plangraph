import asyncio
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from plangraph.routing_support.leg_state import LegStateStore


def test_down_expiry_and_recovery(tmp_path):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    assert store.last_probe_ts('agy/model') is None
    store.record_probe_fail('agy/model', 'timeout', 'leg-probe', now=100, down_s=20)
    state = store.leg_status('agy/model', now=110)
    assert state['state'] == 'down'
    assert state['evidence']['body_excerpt'] == 'timeout'
    assert store.last_probe_ts('agy/model') == 100
    assert store.leg_status('agy/model', now=120)['state'] == 'reprobe'
    store.record_probe_pass('agy/model', 'leg-probe', now=121)
    assert store.leg_status('agy/model', now=121)['state'] == 'ok'
    assert store.last_probe_ts('agy/model') == 121


def test_probe_module():
    from plangraph.spawner.leg_probe import probe
    assert callable(probe)


@pytest.mark.parametrize('success', [True, False])
def test_commit_probe_and_timeout(monkeypatch, success):
    from plangraph.spawner.leg_probe import probe, key
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.routing_support import leg_state
    leg = next(l for l in DEFAULT_LEGS if l.harness == 'agy' and 'implement' in l.roles)
    roots = []
    async def factory(*args, **kwargs):
        roots.append(kwargs['cwd'])
        if success:
            code = "from pathlib import Path; import subprocess; Path('ok.txt').write_text('ok'); subprocess.run(['git','add','ok.txt'],check=True); subprocess.run(['git','-c','commit.gpgsign=false','commit','-m','probe'],check=True)"
        else:
            code = 'import time; time.sleep(60)'
        return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)
    runner = SpawnerRunner(process_factory=factory)
    result = asyncio.run(probe(runner, leg, timeout_s=3 if success else .3))
    assert result['ok'] is success
    assert result['reason'] == ('' if success else 'timeout')
    assert leg_state.leg_status(key(leg))['state'] == ('ok' if success else 'down')
    assert roots and not Path(roots[0]).exists()
    if not success:
        from plangraph.planner.routing import plan_spawn
        plan = plan_spawn({'estimate_tokens': 1000}, role='implement', legs=(leg,))
        assert not plan.legs
        assert plan.skipped[0]['reason'] == 'down'


def test_ttl_and_writer_only(monkeypatch):
    from plangraph.spawner import leg_probe
    leg = SimpleNamespace(model='agy/model', harness='agy')
    calls = []
    async def fake(*args, **kwargs):
        calls.append(args)
        return dict(ok=False, reason='timeout', seconds=0)
    monkeypatch.setattr(leg_probe, 'probe', fake)
    async def run():
        assert await leg_probe.before_launch(None, leg, 'implement', False) is None
        monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
        assert await leg_probe.before_launch(None, leg, 'review', False) is None
        assert await leg_probe.before_launch(None, leg, 'implement', True) is None
        assert await leg_probe.before_launch(None, leg, 'implement', False) == 'down: timeout'
        leg_probe.leg_state.record_probe_pass('agy/model', 'leg-probe')
        assert await leg_probe.before_launch(None, leg, 'implement', False) is None
    asyncio.run(run())
    assert len(calls) == 1


def test_execute_probe_falls_through(tmp_path, monkeypatch):
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import SpawnerRunner, spawn_service, JobInfo
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    service = spawn_service(runner, state=tmp_path / 'state')
    probed, launched = [], []
    async def fake_probe(runner, leg, **kwargs):
        probed.append(leg.id)
        return dict(ok=len(probed) > 1, reason='timeout', seconds=0)
    async def spawn(**kwargs):
        launched.append(kwargs['model_or_combo'])
        return JobInfo(job_id='fake', status='completed', task_prompt='',
                       model_or_combo=kwargs['model_or_combo'], exit_code=0)
    monkeypatch.setattr(leg_probe, 'probe', fake_probe)
    monkeypatch.setattr(runner, 'spawn', spawn)
    record = asyncio.run(service.spawn(card={'estimate_tokens': 1000}, prompt='work', wait_seconds=None))
    assert record['status'] == 'completed'
    assert len(probed) == 2 and len(launched) == 1
    assert record['attempts'][0]['status'] == 'skipped'
    assert record['attempts'][0]['skip_reason'] == 'down: timeout'
