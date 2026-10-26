import asyncio
import codecs
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner import resolver, stall_watchdog
from plangraph.spawner.env_redact import unavailable_reason
from plangraph.spawner.runner import JobInfo, spawn_service


@pytest.mark.parametrize('code', [None, 1])
@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_windows_marker(code, stream):
    text = '\x1b[91m\x1b[1mError: \x1b[0mModel unavailable: fixture/model\r\n'
    assert unavailable_reason(code, text if stream == 'stdout' else '',
                              text if stream == 'stderr' else '') == 'Error: Model unavailable: fixture/model'


@pytest.mark.parametrize('code', [None, 1])
def test_windows_fallthrough_tails_and_choice(tmp_path, monkeypatch, code):
    legs = tuple(replace(DEFAULT_LEGS[0], id=n, model='fixture/' + n) for n in ('first', 'next'))
    calls = []
    async def spawn(**kw):
        calls.append(kw['model_or_combo'])
        return JobInfo(str(len(calls)), 'failed' if len(calls) == 1 else 'completed',
                       'x', kw['model_or_combo'], exit_code=code if len(calls) == 1 else 0,
                       stdout='' if len(calls) == 1 else 'done',
                       stderr='\x1b[91m\x1b[1mError: \x1b[0mModel unavailable: fixture/first\r\n' if len(calls) == 1 else '')
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = spawn_service(runner=runner, state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan(legs, (), 'test', (), ()))
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert calls == [leg.model for leg in legs]
    assert result['chosen_leg']['id'] == 'next'
    assert result['attempts'][0]['skip_reason'] == 'Error: Model unavailable: fixture/first'
    assert result['stdout_tail'] and result['stderr_tail']


def test_probe_resolves_cmd_and_records_failure(monkeypatch, caplog):
    monkeypatch.setattr(resolver.shutil, 'which', lambda *a, **kw: 'C:/Tools/opencode.cmd')
    calls = []
    def run(command, **kw):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout='', stderr='probe failed')
    monkeypatch.setattr(resolver.subprocess, 'run', run)
    assert resolver._probe_served_models('{}', env={'PATH': 'fixture'}) is None
    assert calls == [['C:/Tools/opencode.cmd', 'models']]
    assert 'host_probe_error' in caplog.text


def test_windows_task_utf16_and_disabled(tmp_path):
    root = tmp_path / 'space & root'
    templates = root / 'deploy/watchdog'
    templates.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / 'deploy/watchdog/watchdog.xml'
    (templates / 'watchdog.xml').write_bytes(source.read_bytes())
    calls = []
    stall_watchdog.install('windows', tmp_path / 'out', python='C:/Program Files/A&B/python.exe',
                           root=root, disabled=True, run=lambda cmd, **kw: calls.append(cmd))
    data = (tmp_path / 'out/plangraph-stall-watchdog.xml').read_bytes()
    assert data.startswith(codecs.BOM_UTF16_LE)
    assert 'encoding="UTF-16"' in data.decode('utf-16')
    tree = ET.fromstring(data)
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert tree.find('.//t:Command', ns).text == 'C:/Program Files/A&B/python.exe'
    assert tree.find('.//t:Arguments', ns).text == '-m plangraph.spawner.stall_watchdog tick'
    assert tree.find('.//t:WorkingDirectory', ns).text == str(root) + '\\src'
    assert calls[-1] == ['schtasks', '/Change', '/TN', 'Plangraph Stall Watchdog', '/DISABLE']


def test_unattempted_leg_never_chosen(tmp_path, monkeypatch):
    legs = tuple(replace(DEFAULT_LEGS[0], id=n, model='fixture/' + n) for n in ('first', 'next'))
    calls = []
    async def spawn(**kw):
        calls.append(kw['model_or_combo'])
        if len(calls) == 2:
            raise OSError('fixture launch failed')
        return JobInfo('first', 'failed', 'x', legs[0].model, exit_code=None,
                       stderr='Error: Model unavailable: fixture/first\r\n')
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = spawn_service(runner=runner, state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan(legs, (), 'test', (), ()))
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert result['chosen_leg'] is None
    assert result['stderr_tail']


def test_windows_process_cmd_resolution(tmp_path, monkeypatch):
    import os
    import shutil
    from plangraph.spawner.runner import SpawnerRunner
    calls = []
    async def factory(*args, **kw):
        calls.append((args, kw))
        async def communicate():
            return b'', b'\x1b[91mError: \x1b[0mModel unavailable: fixture/model\r\n'
        return SimpleNamespace(pid=123, returncode=None, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    monkeypatch.setattr(shutil, 'which', lambda *a, **kw: 'C:/Tools/opencode.cmd')
    async def probe(*a, **kw):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    async def run():
        # Limit the platform patch to the process launch; pathlib on Linux
        # cannot construct WindowsPath objects.
        with monkeypatch.context() as patch:
            from plangraph.spawner import runner as runner_module
            patch.setattr(runner_module, 'os', SimpleNamespace(name='nt', environ=os.environ, getcwd=os.getcwd))
            job = await runner.spawn(prompt='fixture', model_or_combo=DEFAULT_LEGS[0].model, client='opencode')
        await runner._tasks[job.job_id]
        assert unavailable_reason(job.exit_code, job.stdout, job.stderr)
        assert job.stderr
    asyncio.run(run())
    assert calls[0][0][0] == 'C:/Tools/opencode.cmd'
    assert 'start_new_session' not in calls[0][1]
