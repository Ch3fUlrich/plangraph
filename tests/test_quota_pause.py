"""Hermetic quota admission checks; no model processes are started."""
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plangraph.routing_errors import SpawnRefused, ParameterRefused
from plangraph.spawner.sessions import Sessions
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.mark.parametrize('action', ['launch', 'relaunch'])
@pytest.mark.parametrize('dry', [False, True])
def test_session_pause_and_reset(tmp_path, monkeypatch, action, dry):
    sessions = Sessions(state_root=tmp_path)
    monkeypatch.setattr(sessions, '_names_mode', lambda: 'free')
    calls = []
    monkeypatch.setattr(sessions, '_launch_inner', lambda *a: calls.append(a) or {'ok': True})
    monkeypatch.setattr(sessions, '_relaunch_inner', lambda *a: calls.append(a) or {'ok': True})

    def launch(name):
        if dry:
            return sessions._action(action, name, lambda: calls.append(name) or {'ok': True}, dry=True)
        if action == 'relaunch':
            return sessions.relaunch(name)
        return sessions.launch(name=name, cwd=str(tmp_path), model='test', effort='low',
                               permission_mode='default', mcp_config='test', prompt='test')

    flag = tmp_path / 'quota-pause'
    flag.touch()
    with pytest.raises(ParameterRefused, match='quota pause'):
        launch('implement-0001')
    assert not calls
    if dry:
        assert not sessions._sessions.exists()
    assert launch('basic-analysis-0001')['ok']
    flag.unlink()
    assert launch('implement-0001')['ok']
    assert len(calls) == 2


@pytest.mark.parametrize('dry', [False, True])
def test_action_run_reraises_parameter_refused_and_only_skips_dry_audit(tmp_path, monkeypatch, dry):
    sessions = Sessions(state_root=tmp_path)
    audit = Mock()
    monkeypatch.setattr(sessions, '_audit_guarded', audit)
    refusal = ParameterRefused('injected refusal')
    action = Mock(side_effect=refusal)
    with pytest.raises(ParameterRefused) as exc:
        sessions._action_run('launch', 'implement-0001', action, dry)
    assert exc.value is refusal
    action.assert_called_once_with()
    if dry:
        audit.assert_not_called()
    else:
        audit.assert_called_once_with('launch', 'implement-0001', False, str(refusal),
                                      None, sessions._audit_argv, refusal)
    assert not sessions._sessions.exists()


@pytest.mark.parametrize('state_attr', ['state', '_state', None])
def test_claude_seat_pause_and_reset(tmp_path, monkeypatch, state_attr):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path / 'unrelated'))
    service = SeatSpawnMixin()
    if state_attr:
        setattr(service, state_attr, tmp_path)
    else:
        monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
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


@pytest.mark.parametrize('source', ['state', 'xdg', 'home', 'empty'])
@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling-link', 'link-loop'])
def test_watchdog_skips_with_one_alert(tmp_path, source, kind):
    home = tmp_path / 'manager-home'
    root = {'state': tmp_path / 'explicit state', 'xdg': tmp_path / 'xdg/plangraph',
            'home': home / '.local/state/plangraph', 'empty': home / '.local/state/plangraph'}[source]
    root.mkdir(parents=True)
    command = _watchdog_command(home)
    env = {'PATH': '/usr/bin:/bin'}  # HOME absent: systemd %h is authoritative.
    if source == 'state':
        env.update(PLANGRAPH_SPAWN_STATE=str(root), XDG_STATE_HOME=str(tmp_path / 'wrong'))
    elif source == 'xdg':
        env.update(PLANGRAPH_SPAWN_STATE='', XDG_STATE_HOME=str(tmp_path / 'xdg'))
    elif source == 'empty':
        env.update(PLANGRAPH_SPAWN_STATE='', XDG_STATE_HOME='', HOME=str(tmp_path / 'wrong'))
    flag = root / 'quota-pause'
    if kind == 'directory':
        flag.mkdir()
    elif kind in ('dangling-link', 'link-loop'):
        flag.symlink_to(tmp_path / 'missing' if kind == 'dangling-link' else flag)
    else:
        flag.touch()
    result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ['ctx-watchdog: quota pause; relaunch skipped']
    assert not result.stderr
    flag.rmdir() if kind == 'directory' else flag.unlink()
    result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert not result.stdout and not result.stderr


def _watchdog_command(home):
    unit = (Path(__file__).resolve().parents[1] /
            'deploy/ctx-watchdog/ctx-watchdog.service.in').read_text()
    condition = next(line.split('=', 1)[1] for line in unit.splitlines()
                     if line.startswith('ExecCondition='))
    return shlex.split(condition.replace('$$', '$').replace('%h', str(home)))


@pytest.mark.parametrize('kind', ['unreadable', 'unsearchable', 'missing', 'file',
                                 'dangling-link', 'link-loop'])
def test_watchdog_uninspectable_state_fails_closed(tmp_path, kind):
    root = tmp_path / 'state'
    if kind in ('unreadable', 'unsearchable'):
        if os.name != 'posix' or os.geteuid() == 0:
            pytest.skip('directory permission checks need an unprivileged POSIX user')
        root.mkdir()
        root.chmod(0o300 if kind == 'unreadable' else 0o600)
    elif kind == 'file':
        root.touch()
    elif kind in ('dangling-link', 'link-loop'):
        root.symlink_to(tmp_path / 'missing' if kind == 'dangling-link' else root)
    try:
        result = subprocess.run(_watchdog_command(tmp_path), cwd=tmp_path,
                                env={'PATH': '/usr/bin:/bin', 'PLANGRAPH_SPAWN_STATE': str(root)},
                                capture_output=True, text=True)
        assert result.returncode == 1
        assert result.stdout.splitlines() == ['ctx-watchdog: quota pause; relaunch skipped']
        assert not result.stderr
    finally:
        if kind in ('unreadable', 'unsearchable'):
            root.chmod(0o700)


@pytest.mark.parametrize('dry', [False, True])
@pytest.mark.parametrize('source', ['state', '_state', 'state_root', 'env'])
def test_judge_pause_precedes_store_resolution(tmp_path, monkeypatch, dry, source):
    import asyncio
    from plangraph.spawner import leg_probe
    runner = SimpleNamespace()
    if source == 'env':
        monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    else:
        setattr(runner, source, tmp_path)
        monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path / 'unrelated'))
    resolve_store = Mock(side_effect=OSError('injected store failure'))
    monkeypatch.setattr(leg_probe, '_store_for', resolve_store)
    (tmp_path / 'quota-pause').touch()
    leg = SimpleNamespace(id='claude-test', model='claude-test')
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'review', dry)) == 'quota pause'
    resolve_store.assert_not_called()
    (tmp_path / 'quota-pause').unlink()
    with pytest.raises(OSError, match='injected store failure'):
        asyncio.run(leg_probe.before_launch(runner, leg, 'review', dry))
    resolve_store.assert_called_once_with(runner)


def test_claude_judge_admission_before_probe(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    store = SimpleNamespace(leg_status=Mock(return_value={'state': 'ok'}),
                            last_probe_ts=Mock(return_value=None))
    monkeypatch.setattr(leg_probe, '_store_for', lambda runner: store)
    probe = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(leg_probe, 'probe', probe)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    runner = SimpleNamespace(state=tmp_path)
    leg = SimpleNamespace(id='claude-test', model='claude-test')
    (tmp_path / 'quota-pause').touch()
    for dry in (False, True):
        assert asyncio.run(leg_probe.before_launch(runner, leg, 'review', dry)) == 'quota pause'
        assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', dry)) == 'quota pause'
    store.leg_status.assert_not_called()
    probe.assert_not_awaited()
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'basic-analysis', False)) is None
    store.leg_status.assert_called_once_with('claude-test')
    (tmp_path / 'quota-pause').unlink()
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'review', False)) is None
    assert store.leg_status.call_count == 2
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
    probe.assert_awaited_once_with(runner, leg, store=store)


@pytest.mark.parametrize('dry', [False, True])
@pytest.mark.parametrize('source', ['state', '_state', 'state_root', 'store-root', 'store-path', 'adapter'])
def test_judge_uses_configured_state(tmp_path, monkeypatch, dry, source):
    import asyncio
    from plangraph.spawner import leg_probe
    from plangraph.routing_support.leg_state import LegStateStore
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path / 'unrelated'))
    store = LegStateStore(tmp_path / 'leg-state.sqlite')
    if source == 'store-root':
        store.state_root = tmp_path
    reader = SimpleNamespace(enabled=True, store=store, record_probe_fail=Mock()) if source == 'adapter' else store
    runner = SimpleNamespace(leg_reader=reader)
    if source in ('state', '_state', 'state_root'):
        setattr(runner, source, tmp_path)
        runner.leg_reader = SimpleNamespace(record_probe_fail=Mock())
    monkeypatch.setattr(leg_probe, 'probe', AsyncMock(side_effect=AssertionError('probe while paused')))
    (tmp_path / 'quota-pause').touch()
    leg = SimpleNamespace(id='claude-test', model='claude-test')
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'review', dry)) == 'quota pause'
    assert not store.path.exists()


def test_public_session_dry_run_refuses(tmp_path, monkeypatch):
    sessions = Sessions(state_root=tmp_path)
    monkeypatch.setattr(sessions, '_names_mode', lambda: 'free')
    inner = Mock(side_effect=AssertionError('dry launch bypassed quota gate'))
    monkeypatch.setattr(sessions, '_launch_inner', inner)
    (tmp_path / 'quota-pause').touch()
    with pytest.raises(ParameterRefused, match='quota pause'):
        sessions.launch(name='implement-0001', cwd=str(tmp_path), model='test', effort='low',
                        permission_mode='default', mcp_config='test', prompt='test', dry_run=True)
    inner.assert_not_called()
    assert not sessions._sessions.exists()


def test_paused_relaunch_preserves_running_session(tmp_path, monkeypatch):
    """Exercise the real relaunch body so a gate moved after stop cannot pass."""
    sessions = Sessions(state_root=tmp_path)
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('test')
    record = dict(name='implement-0001', session_id='old-session', state='running',
                  prompt_file=str(prompt), cwd=str(tmp_path), model='test', effort='low',
                  permission_mode='default', mcp='test')
    sessions._write_record(record['name'], record)
    monkeypatch.setattr(sessions, '_names_mode', lambda: 'free')
    monkeypatch.setattr(sessions, '_load_config', lambda: {'session_names': 'free'})
    monkeypatch.setattr(sessions, '_relaunch_handoff', lambda *a: None)
    monkeypatch.setattr(sessions, '_check_cwd', lambda *a: tmp_path)
    monkeypatch.setattr(sessions, '_check_choice', lambda value, *a: value)
    monkeypatch.setattr(sessions, '_check_mcp', lambda *a: tmp_path)
    monkeypatch.setattr(sessions, '_style_text', lambda *a: '')
    monkeypatch.setattr(sessions, '_build_argv', lambda *a: (['claude', 'test'], {}))
    stop, start = Mock(return_value='old-session'), Mock(return_value=('new-session', None))
    monkeypatch.setattr(sessions, '_ensure_stopped', stop)
    monkeypatch.setattr(sessions, '_start', start)
    (tmp_path / 'quota-pause').touch()
    with pytest.raises(ParameterRefused, match='quota pause'):
        sessions.relaunch(record['name'])
    stop.assert_not_called()
    start.assert_not_called()
    assert sessions._find_record(record['name'], None) == record
    (tmp_path / 'quota-pause').unlink()
    assert sessions.relaunch(record['name'])['session_id'] == 'new-session'
    stop.assert_called_once_with(record, record['name'])
    start.assert_called_once()
