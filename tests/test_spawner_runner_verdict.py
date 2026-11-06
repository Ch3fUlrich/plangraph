"""D-4430: denied-tool review verdicts survive opencode's rc 1 termination."""
import asyncio
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
