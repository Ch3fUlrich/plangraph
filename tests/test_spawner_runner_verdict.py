"""D-4430: denied-tool review verdicts survive opencode's rc 1 termination."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import JobInfo, SpawnerRunner


def _monitor(tmp_path, monkeypatch, output, *, denied=True, role='review', client='opencode', rc=1):
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo('verdict-job', 'running', 'review', 'test-model', role=role,
                  command=[client], cwd=str(tmp_path))
    if denied:
        job.events.append({'event': 'opencode_tools_denied'})
    runner.jobs[job.job_id] = job
    witnesses = []

    async def collect(*args):
        return output.encode(), b'Error: terminated'

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr(runner, '_reap_process', reap)
    monkeypatch.setattr(runner, '_check_git_config_guard', lambda *args: None)
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.fail_unchanged_writer', lambda *args: None)
    monkeypatch.setattr('plangraph.spawner.runner._opencode_tools_denied', lambda *args: denied)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.witness',
                        lambda *args: witnesses.append(job.exit_code))
    asyncio.run(runner._monitor_process(job.job_id, SimpleNamespace(returncode=rc), 'test-model'))
    assert job.exit_code == rc
    assert ({'event': 'opencode_tools_denied'} in job.events) == denied
    assert witnesses == ([rc] if denied and role == 'review' and client == 'opencode' else [])
    return job


@pytest.mark.parametrize('verdict', ['ACCEPT', 'REQUEST_CHANGES'])
def test_denied_review_rc1_final_verdict_completed(tmp_path, monkeypatch, verdict):
    job = _monitor(tmp_path, monkeypatch, f'Full review\nVERDICT: {verdict}\n\n')
    assert job.status == 'completed'
    assert not any(event['event'] == 'harness_failed' for event in job.events)


@pytest.mark.parametrize('output', ['Full review', 'VERDICT: ACCEPT\nMore text',
                                     ' VERDICT: ACCEPT', 'VERDICT: ACCEPT ',
                                     'VERDICT: ACCEPTED'])
def test_denied_review_rc1_without_exact_final_verdict_failed(tmp_path, monkeypatch, output):
    assert _monitor(tmp_path, monkeypatch, output).status == 'failed'


@pytest.mark.parametrize('options', [{'denied': False}, {'role': 'implement'},
                                     {'client': 'other'}, {'rc': 2}])
def test_verdict_without_eligible_denied_review_failed(tmp_path, monkeypatch, options):
    assert _monitor(tmp_path, monkeypatch, 'VERDICT: ACCEPT', **options).status == 'failed'


@pytest.mark.parametrize('verdict', ['ACCEPT', 'REQUEST_CHANGES'])
@pytest.mark.parametrize('finished', [False, True])
def test_framed_denied_review_preserves_verdict(tmp_path, monkeypatch, verdict, finished):
    text = f'Full review\nVERDICT: {verdict}'
    output = json.dumps({'type': 'text', 'part': {'text': text}})
    if finished:
        output += '\n' + json.dumps({'type': 'step_finish', 'part': {'reason': 'stop'}})
    job = _monitor(tmp_path, monkeypatch, output)
    assert job.status == 'completed'
    assert job.stdout == text
    assert not any(e['event'] in ('harness_failed', 'terminated_after_answer') for e in job.events)


@pytest.mark.parametrize('rc', [0, 1, 2])
def test_framed_stdout_callers_receive_plain_answer(tmp_path, monkeypatch, rc):
    from plangraph.review.verdict import parse_verdict
    from plangraph.spawner.runner import spawn_service
    from plangraph.spawner.seat_artifact import publish

    text = 'Review complete\nTEST-GAMING: no - searched tests: 0 hits\nVERDICT: ACCEPT'
    output = json.dumps({'type': 'text', 'part': {'text': text}})
    job = _monitor(tmp_path, monkeypatch, output, rc=rc)
    assert job.stdout == text
    assert parse_verdict(job.stdout) == 'pass'
    service = spawn_service(state=tmp_path / 'state')
    service.runner.jobs[job.job_id] = job
    record = dict(run_id='run-framed', job_id=job.job_id, role='review',
                  status=job.status, exit_code=rc, ended=1, attempts=[])
    service._save(record)
    assert record['stdout_tail'] == text
    if rc == 0:
        job.served_model = 'test/model'
        job.prompt_sha256 = 'a' * 64
        job.response_finish_reason = 'stop'
        publish(tmp_path, record, job)
        assert (tmp_path / 'review.txt').read_text() == text
        assert json.loads((tmp_path / 'evidence.json').read_text())['verdict'] == 'pass'
