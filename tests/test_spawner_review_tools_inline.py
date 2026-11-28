"""D-4681: harnesses without repo tools receive review diffs regardless of tool_mode."""
from types import SimpleNamespace
import subprocess

import pytest

from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def diff_ref(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_CONTEXT_STRICT', raising=False)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')

    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git('init', '-q')
    path = tmp_path / 'change.txt'
    path.write_text('before\n')
    git('add', 'change.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    path.write_text('after\n')
    git('add', 'change.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'head')
    return {'repo': str(tmp_path), 'base': base, 'head': git('rev-parse', 'HEAD')}


def prompt(diff_ref, *, harness='opencode', mode='tools', role='review',
           missing=False, max_tokens=100000, document=False):
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=diff_ref['repo'])
    leg = SimpleNamespace(harness=harness, tool_mode=mode, max_request_tokens=max_tokens)
    card = {} if missing else {'diff_ref': diff_ref}
    result = seat._leg_prompt('review this', card, role, leg, document_review=document)
    assert leg.tool_mode == mode
    return result


def test_mimo_service_refuses_invalid_inline_material_before_launch(diff_ref, tmp_path, monkeypatch, admission_memory):
    import asyncio
    from unittest.mock import AsyncMock
    from plangraph.routing_errors import SpawnRefused
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.spawner.service import SpawnService

    admission_memory()
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'https://gateway.invalid/v1')
    process = AsyncMock(side_effect=AssertionError('invalid inline payload reached provider launch'))
    runner = SpawnerRunner(default_cwd=diff_ref['repo'], process_factory=process)
    service = SpawnService(runner=runner, state=tmp_path / 'state')
    monkeypatch.setattr(service, 'client_probe', lambda _: True)

    async def run(card, prompt):
        return await service.spawn(card=dict(card, estimate_tokens=1000),
            prompt=prompt, role='review', writer_families=['gpt'], leg='mimo-v2.6-pro',
            override_reason='Verify inline payload admission', wait_seconds=None)

    missing = asyncio.run(run({}, 'Review this change.'))
    assert missing['status'] == 'failed' and 'no diff_ref' in missing['error']
    with pytest.raises(SpawnRefused, match='empty_diff'):
        asyncio.run(run({'diff_ref': dict(diff_ref, head=diff_ref['base'])}, 'Review this change.'))
    too_large = asyncio.run(run({'diff_ref': diff_ref}, 'x' * 600_001))
    assert too_large['status'] == 'failed' and 'prompt_too_large' in too_large['error']
    process.assert_not_called()


@pytest.mark.parametrize('mode', ['tools', 'inline-only'])
def test_review_diff_inlined(diff_ref, mode):
    out = prompt(diff_ref, mode=mode)
    assert out == inline_diff_prompt('review this', diff_ref, max_chars=300000)
    assert '-before\n+after' in out


def test_tools_missing_diff_ref_unchanged(diff_ref):
    assert prompt(diff_ref, missing=True) == 'review this'


def test_inline_only_missing_diff_ref_refused(diff_ref):
    with pytest.raises(InlineDiffError, match='no diff_ref'):
        prompt(diff_ref, mode='inline-only', missing=True)


@pytest.mark.parametrize('harness', ['claude', 'agy', 'qodercli'])
@pytest.mark.parametrize('missing', [False, True])
def test_real_tool_harness_unchanged(diff_ref, harness, missing):
    assert prompt(diff_ref, harness=harness, missing=missing) == 'review this'


def test_opencode_implementation_unchanged(diff_ref):
    assert prompt(diff_ref, role='implement', missing=True) == 'review this'


def test_document_review_exception_preserved(diff_ref):
    assert prompt(diff_ref, missing=True, document=True) == 'review this'


@pytest.mark.parametrize('mode', ['tools', 'inline-only'])
def test_same_diff_cap(diff_ref, mode):
    from plangraph.spawner.context_pack import ContextError, PromptBoundError
    assert issubclass(PromptBoundError, ContextError)
    assert issubclass(PromptBoundError, InlineDiffError)
    with pytest.raises(PromptBoundError, match='prompt_too_large') as caught:
        prompt(diff_ref, mode=mode, max_tokens=1)
    assert isinstance(caught.value, InlineDiffError)
    assert isinstance(caught.value, ContextError)
    assert isinstance(caught.value, PromptBoundError)
    with pytest.raises(InlineDiffError, match='prompt_too_large: diff too large') as direct:
        inline_diff_prompt('review this', diff_ref, max_chars=3)
    assert direct.value.chars > direct.value.max_chars == 3


def test_tools_invalid_diff_ref_refused(diff_ref):
    with pytest.raises(InlineDiffError):
        prompt(dict(diff_ref, base='nonexistent-revision'))


def test_diff_ref_paths_inlines_only_selected_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (tmp_path / 'keep.txt').write_text('before\n')
    (tmp_path / 'skip.txt').write_text('before\n')
    git('add', 'keep.txt', 'skip.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (tmp_path / 'keep.txt').write_text('keep-after\n')
    (tmp_path / 'skip.txt').write_text('skip-after\n')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qam', 'head')
    ref = {'repo': str(tmp_path), 'base': base, 'head': git('rev-parse', 'HEAD'), 'paths': ['keep.txt']}
    out = inline_diff_prompt('review this', ref, max_chars=100_000)
    assert 'keep-after' in out
    assert 'skip-after' not in out


@pytest.mark.parametrize('bad_paths', [[], [''], ['/abs/path'], ['../secret'], ['a/../b'], ['--output=/tmp/x'], [':(glob)*']])
def test_diff_ref_invalid_paths_refused(diff_ref, bad_paths):
    with pytest.raises(InlineDiffError, match='diff_ref paths'):
        inline_diff_prompt('review this', dict(diff_ref, paths=bad_paths), max_chars=100_000)


@pytest.mark.parametrize('dispatch', ['routed', 'explicit', 'base'])
@pytest.mark.parametrize('scenario, launches, terminal', [
    ('verdict', 2, False), ('twice', 2, True), ('error', 2, True),
    ('no-cap', 1, True), ('normal', 1, False), ('non-review', 1, False),
    ('already-capped', 1, True), ('stderr-error', 2, True),
    ('zero-exit', 2, True), ('cancelled', 1, False),
    ('timeout', 1, False), ('retry-timeout', 2, False), ('deadline', 1, True),
    ('provider-stop', 2, True), ('thin-budget', 1, True),
    ('stalled', 1, False), ('retry-stalled', 2, False), ('retry-cancelled', 2, False),
    ('ordinary-timeout', 1, False), ('ordinary-cancelled', 1, False),
])
def test_reasoning_output_retry(diff_ref, tmp_path, monkeypatch, dispatch, scenario, launches, terminal):
    """Use real spawn/render/monitor/record paths; only transport and witnesses are fake."""
    import asyncio
    import json
    import time
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from plangraph.planner.legs import DEFAULT_LEGS, resolve_harness
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    from plangraph.spawner.service import SpawnService

    chosen = next(l for l in DEFAULT_LEGS if l.id == 'mimo-v2.6-flash')
    if scenario == 'no-cap':
        luna = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6-luna')
        chosen = resolve_harness(luna, installed=lambda harness: harness == 'opencode').leg
    role = 'orchestrate' if scenario == 'non-review' else 'review'
    good = 'Review complete\nFILES READ: diff only\nVERDICT: REQUEST_CHANGES'
    exhausted = json.dumps({'type': 'step_finish', 'part': {
        'reason': 'length', 'tokens': {'output': 0, 'reasoning': 32768}}})
    if scenario == 'error':
        exhausted = json.dumps({'type': 'error', 'error': {'type': 'output-limit',
            'message': 'Response still truncated after two output token limit continuations'}})
    verdict = '\n'.join(json.dumps(e) for e in [
        {'type': 'text', 'part': {'text': good}},
        {'type': 'step_finish', 'part': {'reason': 'stop', 'tokens': {'output': 20, 'reasoning': 0}}}])
    calls, clock = [], []
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time',
                        SimpleNamespace(time=lambda: clock[0] if clock else time.time()))

    async def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=None, returncode=0 if scenario in ('normal', 'zero-exit') or len(calls) == 2 and scenario == 'verdict' else 1)

    async def collect(runner, jid, proc):
        child_env = runner._child_envs[jid]
        assert child_env is calls[-1][1]['env']
        config = json.loads(child_env['OPENCODE_CONFIG_CONTENT'])
        if scenario == 'already-capped':
            assert config['providers']['mimo']['models']['mimo-v2.6-flash']['body']['thinking'] == {'type': 'disabled'}
        successful = scenario == 'normal' or len(calls) == 2 and scenario == 'verdict'
        if scenario in ('cancelled', 'ordinary-cancelled') or scenario == 'retry-cancelled' and len(calls) == 2:
            runner.jobs[jid].status = 'cancelled'
        if scenario in ('timeout', 'stalled', 'ordinary-timeout') or scenario in ('retry-timeout', 'retry-stalled') and len(calls) == 2:
            status = 'stalled' if scenario.endswith('stalled') else 'timeout'
            runner.jobs[jid].status, runner.jobs[jid].exit_code = status, 6 if status == 'stalled' else 124
            runner.jobs[jid].events.append({'event': 'watchdog_' + status, 'rule': 'max'})
        if scenario in ('deadline', 'thin-budget'):
            clock[:] = [time.time() + (2000 if scenario == 'deadline' else 1475)]
        if scenario == 'provider-stop':
            runner.provider_responses[jid] = {'status': 429}
        if scenario == 'stderr-error':
            return b'', b'Error: output-limit: Response still truncated after two output token limit continuations'
        if scenario.startswith('ordinary-'):
            return b'', b''
        return (verdict if successful else exhausted).encode(), b''

    async def ready(*args, **kwargs):
        return None

    async def launch(plan, **kwargs):
        return plan

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', ready)
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', launch)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', ready)
    def witness(runner, jid, job, env):
        job.request_side_model, job.witness_kind = chosen.model, 'hermetic-witness'
        job.served_family = 'fixture-family'
    monkeypatch.setattr('plangraph.spawner.gateway_witness.witness', witness)
    monkeypatch.setattr('plangraph.spawner.runner._opencode_served_model', lambda *a: None)
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'test-credential')
    if scenario == 'already-capped':
        monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', json.dumps({'providers': {'mimo': {
            'package': '@opencode/ai/providers/openai-compatible', 'models': {
                'mimo-v2.6-flash': {'body': {'thinking': {'type': 'disabled'}}}}}}}))
    # The second plan leg must never turn terminal exhaustion into fall-through.
    plan = SpawnPlan((chosen, replace(chosen, id='spare')), (), 'test', (), ())
    runner = SpawnerRunner(default_cwd=diff_ref['repo'], process_factory=factory)
    monkeypatch.setattr(runner, '_reap_process', reap)
    service = (SpawnService if dispatch == 'base' else spawn_service)(runner=runner, state=tmp_path / 'state')
    if scenario == 'no-cap':
        service.client_probe = lambda harness: harness == 'opencode'
    continuation = AsyncMock(wraps=service._continue_writer)
    monkeypatch.setattr(service, '_continue_writer', continuation)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    options = {'leg': chosen.id, 'override_reason': 'hermetic reasoning retry test'} if dispatch == 'explicit' else {}
    card = {'estimate_tokens': 1000, **({'diff_ref': diff_ref} if role == 'review' else {})}
    record = asyncio.run(service.spawn(card, 'Review this change', role=role,
                                     writer_families=['deepseek'], wait_seconds=None, **options))
    assert len(calls) == launches
    assert service.result(record['run_id']) == record
    assert record['ended'] >= record['started'] and not service._running_records and not runner._tasks
    assert (record.get('reason') == 'reasoning_output_limit') == terminal
    real = [a for a in record['attempts'] if a['status'] != 'skipped']
    assert len(real) == launches
    if launches == 2:
        first, second = [json.loads(c[1]['env']['OPENCODE_CONFIG_CONTENT']) for c in calls]
        model = chosen.model.split('/', 1)[1]
        assert 'thinking' not in first['providers']['mimo']['models'][model].get('body', {})
        assert second['providers']['mimo']['models'][model]['body']['thinking'] == {'type': 'disabled'}
        assert first['permission'] == second['permission']
        assert all(c[0][c[0].index('--model') + 1] == chosen.model for c in calls)
        assert runner.jobs[record['job_id']].task_prompt == next(iter(runner.jobs.values())).task_prompt
        assert runner.jobs[record['job_id']]._watchdog_limits['max_s'] < next(iter(runner.jobs.values()))._watchdog_limits['max_s']
        assert record['reasoning_retry'] == 1
        assert any(e['event'] == 'opencode_reasoning_exhausted' for e in record['events'])
        assert any(e['event'] == 'opencode_reasoning_exhausted' for e in real[0]['events'])
    if scenario in ('normal', 'verdict'):
        assert record['status'] == 'completed'
        assert record['stdout_tail'] == good
        assert record.get('reason') is None
    if terminal:
        assert record['status'] == 'failed' and record['exit_code'] != 0
        assert record['writer_family'] == 'fixture-family'
        assert continuation.await_count == 0
        assert all(any(e['event'] == 'opencode_reasoning_exhausted' for e in a['events']) for a in real)
        assert any(e['event'] == 'reasoning_output_limit' for e in record['events'])
        if dispatch == 'routed':
            job = runner.jobs[record['job_id']]
            assert record['request_side_model'] == job.request_side_model and record['witness_kind'] == job.witness_kind
            assert record['served_model_mismatch'] == any(e.get('event') == 'served_model_mismatch' for e in job.events)
            assert record['completion']['event'] == 'FAILED' and record['completion']['reason'] == 'reasoning_output_limit'
            assert record['error'] and record['completion']['rc'] == job.exit_code
            assert all(a['request_side_model'] == chosen.model and a['witness_kind'] == 'hermetic-witness' for a in real)
    if scenario in ('cancelled', 'ordinary-cancelled', 'retry-cancelled'):
        assert record['status'] == 'cancelled'
    if scenario == 'non-review':
        assert not any(e['event'].startswith(('opencode_step_finish', 'opencode_reasoning', 'reasoning_'))
                       for e in record['events'])
    if scenario in ('timeout', 'retry-timeout', 'ordinary-timeout'):
        assert record['status'] == 'timed_out' and record['reason'] == 'timeout'
    if scenario in ('stalled', 'retry-stalled'):
        assert record['status'] == record['reason'] == 'stalled'
    if 'timeout' in scenario or 'stalled' in scenario:
        assert sum(e['event'].startswith('watchdog_') for e in record['events']) == 1


def test_inline_diff_admits_context_paths_and_still_checks_read_requests(diff_ref):
    from plangraph.review.prompt import render_brief
    from plangraph.routing_errors import SpawnRefused

    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=diff_ref['repo'])
    leg = SimpleNamespace(harness='opencode', tool_mode='inline-only', max_request_tokens=100000)
    card = {'diff_ref': diff_ref}
    brief = render_brief('review', {'callers': 'docs/X.md', 'test_output_paths': 'logs/checks.txt'})
    out = seat._leg_prompt(brief + '\nRead change.txt.', card, 'review', leg)
    assert 'docs/X.md' in out and 'logs/checks.txt' in out and '-before\n+after' in out
    with pytest.raises(SpawnRefused, match='toolless_needs_inline'):
        seat._leg_prompt(brief + '\nRead docs/X.md and check its claims.', card, 'review', leg)
