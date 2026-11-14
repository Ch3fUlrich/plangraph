import asyncio
from types import SimpleNamespace as NS

import pytest


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('marker', ['not_served_on_host', 'not served', 'unknown model', 'model unavailable'])
def test_task_marker_never_books(tmp_path, stream, marker):
    from plangraph.spawner.exit_outcome import fallthrough_reason
    from plangraph.planner.legs import Leg
    calls = []
    service = NS(leg_reader=NS(record_harness_limit=lambda *a, **kw: calls.append(a)))
    leg = Leg('sample', 'subscription', 'opencode', 'provider/model', 'synthetic', (), 1, False)
    job = NS(stdout='', stderr='', exit_code=1, status='failed', events=[])
    setattr(job, stream, "reason = '" + marker + ": ' + model")
    assert fallthrough_reason(service, leg, job, {}) is None
    assert calls == []
    if stream == 'stderr' and marker == 'not_served_on_host':
        job.stderr = 'not_served_on_host: provider/model'
        assert fallthrough_reason(service, leg, job, {}) is not None
        assert len(calls) == 1
        calls.clear()
    setattr(job, stream, 'Error: ' + marker + ': provider/model')
    assert fallthrough_reason(service, leg, job, {}) is not None
    assert len(calls) == 1


@pytest.mark.parametrize('stopped,status', [(False, 'failed'), (True, 'failed'), (True, 'completed')])
def test_failed_review_keeps_leg_not_banner(tmp_path, monkeypatch, stopped, status):
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.legs import Leg
    from plangraph.planner.routing import SpawnPlan
    leg = Leg('sample', 'subscription', 'opencode', 'provider/model', 'synthetic', (), 1, False)
    runner = NS(_processes={}, _tasks={}, provider_responses={}, jobs={})
    async def spawn(**kw):
        job = NS(job_id='sample', status=status, exit_code=0 if status == 'completed' else 1,
                 stdout='VERDICT: CHANGES\nTEST_GAMING: NO\n',
                 stderr='> build · provider/model\nError: transport failed',
                 served_model=None, served_family=None, served_witness_reason=None, events=[])
        runner.jobs[job.job_id] = job
        return job
    runner.spawn = spawn
    service = SpawnService(runner, state=tmp_path)
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', _no_probe)
    if stopped:
        runner.provider_responses['sample'] = {'stop': True}
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'review')
    record = asyncio.run(service._spawn(card={'title': 'synthetic'}, role='review',
        wait_seconds=None, _plan=SpawnPlan((leg,), (), 'test', (), ())))
    assert record['chosen_leg'] is None
    assert record['status'] == record['attempts'][0]['status'] == status
    if stopped and status == 'failed':
        assert record['attempts'][0]['skip_reason'] == 'provider_stop'
    if status == 'completed':
        assert record['attempts'][0]['skip_reason'] is None
        assert record.get('reason') is None
    if not stopped:
        assert record['reason'] == 'Error: transport failed'
        assert record['attempts'][0]['skip_reason'] == 'Error: transport failed'
    assert 'VERDICT: CHANGES' in record['attempts'][0]['stdout_tail']


async def _no_probe(*a, **kw):
    return None


def test_watchdog_entrypoint_builds_hung_from_config(tmp_path, monkeypatch):
    from plangraph.spawner import stall_watchdog as w
    from plangraph.spawner.hung_capture import HungBoundary
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setattr(w, 'read_runs', lambda *a, **kw: [])
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    seen = []
    def tick(*a, **kw):
        if 'state' in kw:
            assert kw.get('stuck') is True
            seen.append(kw.get('hung'))
        return []
    monkeypatch.setattr(w, 'tick', tick)
    config = dict(reader=NS(enabled=True), sample=lambda r: {}, identity=lambda p, i: False,
                  cancel=lambda r: False, fallthrough=lambda r: None, minutes=2)
    assert w.main(['tick', '--dry-run'], hung_config=config) == 0
    assert isinstance(seen[0], HungBoundary) and seen[0].enabled
    assert seen[0].seconds == 120
    assert w.main(['tick', '--dry-run']) == 0
    assert seen[1] is None
