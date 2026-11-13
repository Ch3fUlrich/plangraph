"""Recorded text-call output must advance the real dispatch loop, without a harness."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner.exit_outcome import fallthrough_reason, terminal_reason
from plangraph.spawner.runner import JobInfo, spawn_service

CALLS = ('<tool_call><function=read><parameter=filePath>/work/a.md</parameter>'
         '</function></tool_call><tool_call><function=read><parameter=filePath>'
         '/work/b.md</parameter></function></tool_call>')
RECORDED = '\x1b[0m\n> build · mimo-v2.6-flash\n' + CALLS


@pytest.mark.parametrize('role', ['review', 'implement'])
@pytest.mark.parametrize('fallback', [False, True])
def test_recorded_output_fails_and_falls_through(tmp_path, role, fallback, monkeypatch):
    async def launch(plan, **kwargs):
        return plan
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', launch)
    legs = tuple(replace(DEFAULT_LEGS[0], id=model, model=model, harness='opencode')
                 for model in ('first', 'second'))

    class Runner:
        default_cwd = str(tmp_path)

        def __init__(self):
            self.jobs, self._processes, self._tasks, self.provider_responses = {}, {}, {}, {}

        async def spawn(self, **kwargs):
            jid = str(len(self.jobs))
            job = JobInfo(jid, 'completed', '', kwargs['model_or_combo'],
                          role=role, cwd=str(tmp_path))
            job.command = ['opencode']
            job.exit_code, job.stdout = 0, RECORDED if not self.jobs else 'VERDICT: PASS'
            if role == 'implement' and not self.jobs:
                job.status, job.exit_code = 'failed', 1
                job.events.append({'event': 'writer_no_changes'})
            self.jobs[jid] = job
            return job

    async def run():
        service = spawn_service(runner=Runner(), state=tmp_path / 'state')
        service._route = lambda *a, **kw: SpawnPlan(legs if fallback else legs[:1], (), 'test', (), ())
        return await service.spawn({'estimate_tokens': 1000, 'item': 'demo:REVIEW'}, role=role, wait_seconds=None)

    record = asyncio.run(run())
    assert record['attempts'][0]['status'] == 'failed'
    assert record['attempts'][0]['skip_reason'] == 'tool_call_only'
    assert len(record['attempts']) == (2 if fallback else 1)
    assert record['status'] == ('completed' if fallback else 'failed')
    assert record.get('reason') == (None if fallback else 'tool_call_only')


def job(text=RECORDED, **kwargs):
    return SimpleNamespace(stdout=text, stderr='', exit_code=0, status='completed',
                           command=['opencode'], role='implement', events=[], **kwargs)


@pytest.mark.parametrize('text', ['Done.', 'VERDICT: PASS', CALLS + '\nVERDICT: PASS',
                                  'Example: ' + CALLS, 'I quoted `<tool_call>` here.',
                                  CALLS + '\nReal explanation.\n' + CALLS])
def test_real_message_unaffected(text):
    attempt, record = job(text), {}
    assert fallthrough_reason(None, None, attempt, record) is None
    assert attempt.status == 'completed' and record == {}


def test_real_commit_unaffected(monkeypatch):
    attempt = job(cwd='/unused', writer_email='writer')
    attempt.events = [{'event': 'writer_baseline', 'commits': ['old']}]
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.writer_commits',
                        lambda *args: {'old', 'new'})
    assert fallthrough_reason(None, None, attempt, {}) is None
    assert attempt.status == 'completed'


@pytest.mark.parametrize('reason', ['git_config_mutated', 'cancelled', 'kill_incomplete'])
def test_terminal_verdict_preserved(reason):
    attempt, record = job(), {'reason': reason, 'status': 'failed', 'exit_code': 1}
    assert fallthrough_reason(None, None, attempt, record) is None
    terminal_reason(record, attempt)
    assert record['reason'] == reason
