"""The OOM wrapper's telemetry is an event, never stderr content or a failure reason."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner.exit_outcome import failure_reason, terminal_reason
from plangraph.spawner.runner import JobInfo, SpawnerRunner

OOM = '{"plangraph_oom": {"event": "oom_score_adj", "value": 500}}\n'


async def monitor_job(tmp_path, monkeypatch, name, stderr, stdout='', command='claude'):
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo(name, 'running', 'review', 'm', role='review', command=[command], cwd=str(tmp_path))
    runner.jobs[name] = job

    async def collect(*args):
        return stdout.encode(), stderr.encode()

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr(runner, '_reap_process', reap)
    monkeypatch.setattr(runner, '_check_git_config_guard', lambda *a: None)
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.fail_unchanged_writer', lambda *a: None)
    await runner._monitor_process(name, SimpleNamespace(returncode=1), 'm')
    return job


def run_job(*args, **kwargs):
    return asyncio.run(monitor_job(*args, **kwargs))


def test_oom_line_is_event_only_and_each_attempt_keeps_its_reason(tmp_path, monkeypatch):
    attempts = []
    for i, real in enumerate(('error: model not found', 'error: auth rejected')):
        job = run_job(tmp_path, monkeypatch, f'j{i}', OOM + real + '\n')
        assert {'event': 'oom_score_adj'} in job.events
        assert 'plangraph_oom' not in job.stderr
        assert job.stderr == real + '\n'
        attempts.append(dict(status='failed', skip_reason=failure_reason(job), stderr_tail=job.stderr))
    assert [a['skip_reason'] for a in attempts] == ['error: model not found', 'error: auth rejected']
    record = dict(status='failed', exit_code=1, attempts=attempts,
                  stderr_tail=''.join(a['stderr_tail'] for a in attempts))
    terminal_reason(record)
    assert record['reason'] == 'error: auth rejected'
    assert 'plangraph_oom' not in record['stderr_tail']


def test_oom_only_stderr_gives_exit_code_reason_not_telemetry(tmp_path, monkeypatch):
    job = run_job(tmp_path, monkeypatch, 'j', OOM)
    assert job.stderr == '' and {'event': 'oom_score_adj'} in job.events
    assert failure_reason(job) == 'harness exit code 1'


@pytest.mark.parametrize('extra', ['', ', "meta": "wrapper"'])
def test_known_telemetry_variants_are_removed_but_extra_diagnostics_remain(tmp_path, monkeypatch, extra):
    text = 'wrapper: {"plangraph_oom":{"event":"oom_score_adj","value":500}' + extra + '}\n'
    job = run_job(tmp_path, monkeypatch, 'variants', text + 'error: auth rejected\n')
    assert 'plangraph_oom' not in job.stderr and 'error: auth rejected' in job.stderr
    if extra:
        assert any('meta' in e.get('detail', '') for e in job.events)


def test_unknown_oom_envelope_is_retained_as_diagnostic(tmp_path, monkeypatch):
    text = '{"plangraph_oom":{"event":"arbitrary failure","error":"actual error"}}\n'
    job = run_job(tmp_path, monkeypatch, 'unknown', text)
    assert job.stderr == text and failure_reason(job) == text.strip()


def test_structured_error_survives_display_text_decoding(tmp_path, monkeypatch):
    stdout = ('{"type":"text","part":{"text":"partial answer"}}\n'
              '{"type":"error","error":{"type":"provider.transport","message":"ECONNREFUSED"}}\n')
    job = run_job(tmp_path, monkeypatch, 'partial', OOM, stdout, 'opencode')
    assert job.stdout == 'partial answer' and failure_reason(job) == 'ECONNREFUSED'


CAPTURE = json.loads((Path(__file__).parent / 'fixtures/spawner_dell_oom.json').read_text())


@pytest.mark.parametrize('capture', CAPTURE, ids=lambda c: c['run_id'])
def test_recorded_transport_error_is_the_failure(capture):
    failed = next(a for a in capture['attempts'] if a['status'] == 'failed')
    job = SimpleNamespace(command=['opencode'], stdout=failed['stdout_tail'], stderr=failed['stderr_tail'], exit_code=1)
    assert failure_reason(job) == 'ECONNREFUSED: Unable to connect. Is the computer able to access the url?'


from test_spawner_seat_fallthrough import recorded_plan, replay


def test_production_record_keeps_each_failure_and_names_the_last(replay):
    errors = {leg.id: f'error: failure at {leg.id}' for leg in replay.plan.legs}
    record = replay.run(failures=errors)
    saved = json.loads(replay.service._path(record['run_id']).read_text())
    real = [a for a in saved['attempts'] if a['status'] != 'skipped']
    assert [a['skip_reason'] for a in real] == [errors[leg.id] for leg in replay.plan.legs]
    assert saved['reason'] == errors[replay.plan.legs[-1].id]


@pytest.mark.parametrize('capture', CAPTURE, ids=lambda c: c['run_id'])
def test_incident_capture_through_monitor_and_production_record(tmp_path, monkeypatch, capture):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.service import SpawnService
    failed = next(a for a in capture['attempts'] if a['status'] == 'failed')
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == failed['leg'])
    skipped = tuple(dict(leg=a['leg'], reason=a['skip_reason']) for a in capture['attempts']
                    if a['status'] == 'skipped')
    plan = SpawnPlan((chosen,), skipped, 'incident-replay', (), ())
    runner = SimpleNamespace(default_cwd=str(tmp_path), jobs={}, provider_responses={}, _tasks={}, _processes={})
    service = SpawnService(runner, state=tmp_path / 'state')
    monkeypatch.setattr(service, '_annotate_spawn', lambda *a, **kw: None)
    async def prepare(*a, **kw):
        return 'review fixture'
    monkeypatch.setattr(service, '_prepare_leg_prompt', prepare)
    async def launch(*args, **kwargs):
        job = await monitor_job(tmp_path, monkeypatch, 'incident', failed['stderr_tail'], failed['stdout_tail'], 'opencode')
        runner.jobs[job.job_id] = job
        return job
    monkeypatch.setattr(service, '_launch_leg', launch)
    result = asyncio.run(service._spawn(card={'title': 'incident replay'}, role='review', _plan=plan, wait_seconds=None))
    saved = json.loads(service._path(result['run_id']).read_text())
    assert saved['reason'].startswith('ECONNREFUSED:')
    assert all(a.get('skip_reason') for a in saved['attempts'])
    assert 'plangraph_oom' not in saved['stderr_tail']
    assert next(a for a in saved['attempts'] if a['status'] == 'failed')['skip_reason'].startswith('ECONNREFUSED:')
