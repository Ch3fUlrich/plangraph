"""Read-only reviews must not scan worktrees or open Windows consoles."""
import asyncio
import os
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plangraph.spawner import inline_diff, run_watchdog
from plangraph.spawner.runner import JobInfo, SpawnerRunner


def test_inline_diff_uses_closed_stdin_and_hidden_windows_console(monkeypatch, tmp_path):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='+changed\n', stderr='')

    monkeypatch.setattr(inline_diff.subprocess, 'run', run)
    result = inline_diff.inline_diff_prompt('review',
        {'repo': str(tmp_path), 'base': 'HEAD~1', 'head': 'HEAD'}, max_chars=1000)
    assert '+changed' in result
    assert seen.get('stdin') == subprocess.DEVNULL
    if os.name == 'nt':
        assert seen.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW


def test_review_spawn_skips_edit_baseline_and_hides_console(monkeypatch, tmp_path):
    def forbidden_scan(_cwd):
        pytest.fail('a read-only review must not scan the worktree for edits')

    monkeypatch.setattr(run_watchdog, 'edit_signature', forbidden_scan)
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async',
                        AsyncMock(return_value=None))
    proc = SimpleNamespace(returncode=0, pid=None, stdin=None,
                           communicate=AsyncMock(return_value=(b'VERDICT: ACCEPT', b'')))
    factory = AsyncMock(return_value=proc)

    async def run():
        runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
        job = await runner.spawn('review a tiny fixture', role='review',
            model_or_combo='agy/gemini-3.8-flash-tiered-high', client='agy', wait_seconds=1)
        assert job.status == 'completed'
        assert job._watchdog_baseline is None
        assert factory.await_count == 1
        kwargs = factory.call_args.kwargs
        if os.name == 'nt':
            assert kwargs.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW
        else:
            assert kwargs.get('start_new_session') is True

    asyncio.run(run())


def test_writer_spawn_keeps_edit_baseline(monkeypatch, tmp_path):
    baseline = ((1, 2, 3), b'head')
    scan = lambda _cwd: baseline
    monkeypatch.setattr(run_watchdog, 'edit_signature', scan)
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async',
                        AsyncMock(return_value=None))
    proc = SimpleNamespace(returncode=0, pid=None, stdin=None,
                           communicate=AsyncMock(return_value=(b'done', b'')))
    factory = AsyncMock(return_value=proc)

    async def run():
        runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
        job = await runner.spawn('write a fixture', role='implement',
            model_or_combo='agy/gemini-3.8-flash-tiered-high', client='agy', wait_seconds=1)
        assert job._watchdog_baseline == baseline

    asyncio.run(run())


def test_review_deadline_does_not_wait_for_edit_or_cpu_sampling(monkeypatch, tmp_path):
    def forbidden_scan(*_args):
        pytest.fail('review deadlines must not wait for worktree/CPU sampling')

    monkeypatch.setattr(run_watchdog, 'edit_signature', forbidden_scan)
    monkeypatch.setattr(run_watchdog, 'cpu_seconds', AsyncMock(side_effect=forbidden_scan))

    async def run():
        job = JobInfo('j', 'running', 'review', 'fixture/model', role='review', cwd=str(tmp_path))
        job._watchdog_limits = run_watchdog.thresholds({'max_min': .001}, role='review')
        job._watchdog_limits['interval'] = .01
        job._watchdog_baseline = None
        proc = SimpleNamespace(returncode=None, pid=None, stdin=None,
                               stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())

        async def reap(*_args):
            proc.returncode = 124
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()

        runner = SimpleNamespace(jobs={'j': job}, _child_envs={}, _stdin_payloads={},
                                 default_cwd=str(tmp_path), _reap_process=reap)
        await asyncio.wait_for(run_watchdog.collect(runner, 'j', proc), timeout=1)
        assert job.status == 'timeout'
        assert job.exit_code == 124
        assert any(e.get('rule') == 'max' for e in job.events)

    asyncio.run(run())


@pytest.mark.parametrize('platform,new_session,expected', [
    ('nt', False, {'creationflags': 0x08000000}),
    ('nt', True, {'creationflags': 0x08000200}),
    ('posix', False, {}),
    ('posix', True, {'start_new_session': True}),
])
def test_background_process_options_preserve_platform_isolation(platform, new_session, expected):
    from plangraph.routing_support import background_process_options
    assert background_process_options(new_session=new_session, platform=platform) == expected


@pytest.mark.real_probe  # Exercise the probe function; subprocess.run remains hermetically mocked.
def test_model_probe_uses_closed_stdin_and_hidden_windows_console(monkeypatch):
    from plangraph.routing_support import resolver
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='mimo/mimo-v2.6-flash\n', stderr='')

    monkeypatch.setattr(resolver.subprocess, 'run', run)
    assert resolver._probe_served_models('{}', env={'PATH': os.environ['PATH']}) == frozenset({'mimo/mimo-v2.6-flash'})
    assert seen.get('stdin') == subprocess.DEVNULL
    if os.name == 'nt':
        assert seen.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW


def test_gemini_route_uses_the_served_agy_effort_variant():
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.routing_support.clients import build_standalone_fallback_command
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'agy-gemini-3.8-flash')
    argv = build_standalone_fallback_command('agy', 'review', leg.model, role='review')
    assert argv[argv.index('--model') + 1] == 'gemini-3.8-flash-high'
    explicit = build_standalone_fallback_command('agy', 'review', 'gemini-3.8-flash-medium', role='review')
    assert explicit[explicit.index('--model') + 1] == 'gemini-3.8-flash-medium'


def test_prompt_packaging_does_not_block_the_event_loop(tmp_path):
    from plangraph.spawner.service import SpawnService
    release = threading.Event()
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(tmp_path)), state=tmp_path)
    service._leg_prompt = lambda *a, **kw: 'ready' if release.wait(.3) else 'blocked'

    async def run():
        task = asyncio.create_task(service._prepare_leg_prompt('review', {}, 'review', None,
                                                              deadline=time.time() + 1))
        await asyncio.sleep(.01)
        release.set()
        assert await task == 'ready'

    asyncio.run(run())


def test_prompt_packaging_obeys_the_remaining_review_deadline(tmp_path):
    from plangraph.spawner.inline_diff import InlineDiffError
    from plangraph.spawner.service import SpawnService
    release = threading.Event()
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(tmp_path)), state=tmp_path)
    service._leg_prompt = lambda *a, **kw: release.wait(.5)

    async def run():
        try:
            with pytest.raises(InlineDiffError, match='packaging deadline'):
                await service._prepare_leg_prompt('review', {}, 'review', None,
                                                  deadline=time.time() + .02)
        finally:
            release.set()

    asyncio.run(run())


def test_async_packaging_keeps_invalid_diff_fail_closed(tmp_path):
    from plangraph.spawner.inline_diff import InlineDiffError
    from plangraph.spawner.service import SpawnService
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(tmp_path)), state=tmp_path)
    leg = SimpleNamespace(tool_mode='inline-only', harness='opencode', max_request_tokens=10000)
    card = {'diff_ref': {'repo': str(tmp_path), 'base': 'missing', 'head': 'HEAD'}}

    async def run():
        with pytest.raises(InlineDiffError, match='git diff failed'):
            await service._prepare_leg_prompt('review', card, 'review', leg,
                                              deadline=time.time() + 1)

    asyncio.run(run())


def test_expired_packaging_budget_never_starts_work(tmp_path):
    from plangraph.spawner.inline_diff import InlineDiffError
    from plangraph.spawner.service import SpawnService
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(tmp_path)), state=tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError('expired review must not start packaging')

    service._leg_prompt = forbidden

    async def run():
        with pytest.raises(InlineDiffError, match='packaging deadline'):
            await service._prepare_leg_prompt('review', {}, 'review', None,
                                              deadline=time.time() - 1)

    asyncio.run(run())


def test_packaging_passes_remaining_budget_to_git(tmp_path, monkeypatch):
    from plangraph.spawner import inline_diff
    from plangraph.spawner.service import SpawnService
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(tmp_path)), state=tmp_path)
    leg = SimpleNamespace(tool_mode='tools', harness='opencode', max_request_tokens=10000)
    seen = {}

    def package(prompt, diff_ref, **kwargs):
        seen.update(kwargs)
        return prompt

    monkeypatch.setattr(inline_diff, 'inline_diff_prompt', package)

    async def run():
        assert await service._prepare_leg_prompt('review', {'diff_ref': {}}, 'review', leg,
                                                 deadline=time.time() + 2) == 'review'

    asyncio.run(run())
    assert 0 < seen['timeout'] <= 2
