"""Hermetic quota admission checks; no model processes are started."""
from types import SimpleNamespace

import pytest

from plangraph.routing_errors import SpawnRefused, ParameterRefused
from plangraph.spawner.sessions import Sessions
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.mark.parametrize('action', ['launch', 'relaunch'])
def test_session_pause_and_reset(tmp_path, monkeypatch, action):
    sessions = Sessions(state_root=tmp_path)
    monkeypatch.setattr(sessions, '_names_mode', lambda: 'free')
    calls = []
    monkeypatch.setattr(sessions, '_launch_inner', lambda *a: calls.append(a) or {'ok': True})
    monkeypatch.setattr(sessions, '_relaunch_inner', lambda *a: calls.append(a) or {'ok': True})

    def launch(name):
        if action == 'relaunch':
            return sessions.relaunch(name)
        return sessions.launch(name=name, cwd=str(tmp_path), model='test', effort='low',
                               permission_mode='default', mcp_config='test', prompt='test')

    flag = tmp_path / 'quota-pause'
    flag.touch()
    with pytest.raises(ParameterRefused, match='quota pause'):
        launch('implement-0001')
    assert not calls
    assert launch('basic-analysis-0001')['ok']
    flag.unlink()
    assert launch('implement-0001')['ok']
    assert len(calls) == 2


def test_claude_seat_pause_and_reset(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path / 'unrelated'))
    service = SeatSpawnMixin()
    service.state = tmp_path
    leg = SimpleNamespace(id='claude-test', model='claude-test', tool_mode='tools')
    flag = tmp_path / 'quota-pause'
    flag.touch()
    with pytest.raises(SpawnRefused, match='quota pause'):
        service._leg_prompt('test', {}, 'review', leg)
    assert service._leg_prompt('test', {}, 'basic-analysis', leg) == 'test'
    other = SimpleNamespace(id='other-test', model='other-test', tool_mode='tools')
    assert service._leg_prompt('test', {}, 'review', other) == 'test'
    flag.unlink()
    assert service._leg_prompt('test', {}, 'review', leg) == 'test'


def test_watchdog_skips_with_one_alert(tmp_path):
    import os
    from pathlib import Path
    import shlex
    import subprocess
    unit = (Path(__file__).resolve().parents[1] /
            'deploy/ctx-watchdog/ctx-watchdog.service.in').read_text()
    condition = next(line.split('=', 1)[1] for line in unit.splitlines()
                     if line.startswith('ExecCondition='))
    command = shlex.split(condition.replace('$$', '$'))
    env = dict(os.environ, PLANGRAPH_SPAWN_STATE=str(tmp_path))
    flag = tmp_path / 'quota-pause'
    flag.touch()
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ['ctx-watchdog: quota pause; relaunch skipped']
    flag.unlink()
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert not result.stdout


def test_claude_judge_admission_before_probe(tmp_path):
    import asyncio
    from plangraph.spawner.leg_probe import before_launch
    runner = SimpleNamespace(state=tmp_path)
    leg = SimpleNamespace(id='claude-test', model='claude-test')
    (tmp_path / 'quota-pause').touch()
    assert asyncio.run(before_launch(runner, leg, 'review', False)) == 'quota pause'
    assert asyncio.run(before_launch(runner, leg, 'basic-analysis', True)) is None
    (tmp_path / 'quota-pause').unlink()
    assert asyncio.run(before_launch(runner, leg, 'review', True)) is None
