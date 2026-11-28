"""The scheduled watchdog tick ends a writer's over-long background wait (round telemetry).

Drives ``stall_watchdog.main(['tick'])`` with a fake clock against the real Rust
``pg-spawnd round-watch``, real command processes, the real outbox delivery and a real
``Sessions.relaunch``. The binary comes from the cargo target directory of the checkout
and is rebuilt before the test runs.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _split_ci_environment import timeout_extended

from plangraph.spawner import stall_watchdog as w

T0 = time.time()  # processes started by the test must fall into their launch windows
RUST = Path(__file__).resolve().parents[1] / 'rust'
def _binary():
    target = os.environ.get('CARGO_TARGET_DIR')
    if not target:
        meta = subprocess.run(['cargo', 'metadata', '--format-version', '1', '--no-deps'], cwd=RUST,
                              capture_output=True, text=True, check=True)
        target = json.loads(meta.stdout)['target_directory']
    # Always build: a stale binary from an earlier checkout state would test other code.
    with timeout_extended(1500):
        subprocess.run(['cargo', 'build', '-p', 'pg-spawnd', '--bin', 'pg-spawnd'], cwd=RUST, check=True, timeout=1500)
    return Path(target) / 'debug/pg-spawnd'


pytestmark = pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('cargo'),
                                reason='needs Linux and cargo to build pg-spawnd')


def stamp(seconds):
    return datetime.datetime.fromtimestamp(T0 + seconds, datetime.UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def line(seconds, kind, **extra):
    return json.dumps(dict(type=kind, timestamp=stamp(seconds), **extra))


def request(seconds):
    return line(seconds, 'api-request', id='r%d' % seconds)


def tool(seconds, ident, command, **flags):
    return line(seconds, 'assistant', requestRef='r%d' % int(seconds - 1),
                message=dict(id='m%d' % seconds, stop_reason='tool_use', content=[
                    dict(type='tool_use', id=ident, name='Bash', input=dict(command=command, **flags))]))


def result(seconds, ident, task=None):
    extra = dict(toolUseResult=dict(backgroundTaskId=task)) if task else {}
    return line(seconds, 'user', message=dict(role='user', content=[dict(type='tool_result', tool_use_id=ident, content='ok')]), **extra)


def output_path(root, task):
    return root / 'tasks' / f'{task}.output'


def launch(root, task, command, at):
    """A background start as the harness records it, with the real output path."""
    text = (f'Command running in background with ID: {task}. Output is being written to: '
            f'{output_path(root, task)}. You will be notified when it completes')
    return [request(at - 0.5), tool(at, 'u-' + task, command, run_in_background=True),
            line(at + 0.5, 'user', toolUseResult=dict(backgroundTaskId=task),
                 message=dict(role='user', content=[dict(type='tool_result', tool_use_id='u-' + task, content=text)]))]


def wait_call(task, at):
    """A blocking task-output call: the only wait the watchdog may end a command for."""
    return [request(at - 0.5), line(at, 'assistant', requestRef='r%d' % int(at - 1),
                                    message=dict(id='m%d' % at, stop_reason='tool_use', content=[
                                        dict(type='tool_use', id='w-' + task, name='TaskOutput',
                                             input=dict(task_id=task, block=True))]))]


def command_shell(root, session_id, task, command):
    """A command shell as the harness starts it: own group, session id, output in tasks/<id>.output."""
    tasks = root / 'tasks'
    tasks.mkdir(exist_ok=True)
    out = open(tasks / f'{task}.output', 'wb')
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=session_id)
    return subprocess.Popen(['/bin/sh', '-c', command + '; true'], env=env, stdout=out, stderr=out, start_new_session=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'pg-spawnd').symlink_to(_binary())
    globals()['T0'] = time.time()  # after the build: the shells start within seconds of it
    stop_log = tmp_path / 'claude-stop.log'
    stub = tmp_path / 'claude-stub'
    stub.write_text(f'#!/bin/sh\n[ "$1" = stop ] && echo "$@" >> {stop_log}\nexit 0\n')
    stub.chmod(0o755)
    delivered = tmp_path / 'delivered.log'
    monkeypatch.setenv('PATH', f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path / 'spawn'))
    monkeypatch.setenv('ROUTING_STATE_DIR', str(tmp_path / 'routing'))
    monkeypatch.setenv('PLANGRAPH_CLAUDE_BIN', str(stub))
    monkeypatch.setenv('PLANGRAPH_WATCHDOG_DELIVERY', json.dumps(['/bin/sh', '-c', f'cat >> {delivered}; echo >> {delivered}']))
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [])
    monkeypatch.setattr(w, 'read_activity', lambda state, session: None)
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    clock = [T0]
    monkeypatch.setattr(w.time, 'time', lambda: clock[0])
    sessions = tmp_path / 'routing/launch/sessions'
    sessions.mkdir(parents=True)
    (sessions / 'writer-5.json').write_text(json.dumps(
        dict(name='writer', session_id='sid-5xxxx', cwd='/w/my_proj', session_name='writer-5')))
    transcript = tmp_path / 'home/.claude/projects/-w-my-proj/sid-5xxxx.jsonl'
    transcript.parent.mkdir(parents=True)

    class World:
        root = tmp_path
        spawn = tmp_path / 'spawn'
        stops = stop_log

        def tick(self, minutes, lines):
            transcript.write_text('\n'.join(lines) + '\n')
            clock[0] = T0 + minutes * 60
            assert w.main(['tick']) == 0
            status = self.spawn / 'watchdog/status.txt'
            return status.read_text() if status.exists() else ''

        def delivered(self):
            rows = [json.loads(row) for row in delivered.read_text().splitlines() if row.strip()] if delivered.exists() else []
            return [row for row in rows if row.get('rule') == 'round-wait-timeout']  # the host's own alerts are not ours

        def outbox(self):
            return sorted((self.spawn / 'watchdog/outbox').glob('round-wait-*.json'))

    return World()


def test_the_scheduled_tick_ends_the_task_once_blocks_the_second_wait_and_closes_the_round(world):
    root = world.root
    first = command_shell(root, 'sid-5xxxx', 'bgA', 'sleep 401')
    second = command_shell(root, 'sid-5xxxx', 'bgB', 'sleep 402')
    twin = command_shell(root, 'sid-5xxxx', 'bgZ', 'sleep 401')  # same command text as bgA, another task
    head = [line(0, 'user', promptId='p1', origin=dict(kind='human'), message=dict(role='user', content='go'))]
    one = head + launch(root, 'bgA', 'sleep 401', 2) + launch(root, 'bgB', 'sleep 402', 3) + wait_call('bgA', 4)
    world.tick(1, one)
    assert 'round-wait' not in world.tick(14, one)
    assert first.poll() is None, 'under the cap nothing is stopped'
    status = world.tick(16, one)
    assert 'round-wait-stopped' in status and 'round-wait-notified' in status
    assert first.wait(timeout=20) is not None
    assert twin.poll() is None, 'only the task that was waited on is ended'
    [note] = world.delivered()
    assert note['recipient'] == 'writer-5' and 'sleep 401' in note['text']
    assert not world.outbox(), 'delivered and removed'
    assert 'round-wait' not in world.tick(17, one), 'a later tick adds nothing'
    assert len(world.delivered()) == 1, 'delivered notices are not queued again'

    # bgA is reported ended; the writer now waits on bgB for over fifteen minutes.
    two = one + [line(19 * 60, 'user', origin=dict(kind='task-notification'),
                      message=dict(role='user', content='<task-notification><task-id>bgA</task-id></task-notification>')),
                 result(19 * 60 + 1, 'w-bgA')] + wait_call('bgB', 19 * 60 + 3)
    world.tick(20, two)
    status = world.tick(36, two)
    assert 'round-blocked' in status and 'sleep 402' in status
    assert 'round-session-ended' not in status, 'the writer is not stopped before its notices are delivered'
    assert second.wait(timeout=20) is not None
    assert not world.stops.exists()
    recipients = sorted(e['recipient'] for e in world.delivered()[1:])
    assert recipients == ['routing', 'writer-5'] and all(e['text'].startswith('BLOCKED') for e in world.delivered()[1:])
    assert 'round-session-ended' in world.tick(37, two), 'delivered: the session is ended'
    assert world.stops.read_text() == 'stop sid-5xxx\n'

    # The writer talks on after the close: the round row does not grow past the close.
    three = two + [request(40 * 60), line(41 * 60, 'assistant', requestRef='r%d' % (40 * 60),
                                           message=dict(id='mx', stop_reason='end_turn', content=[dict(type='text', text='x')]))]
    world.tick(42, three)
    rows = [json.loads(row) for row in (world.spawn / 'round-telemetry/rows.jsonl').read_text().splitlines()]
    closed = int((T0 + 36 * 60) * 1000)
    assert rows[0]['outcome'] == 'BLOCKED' and rows[0]['round_key'] == 'writer-5:p1'
    assert rows[0]['closed_ms'] == closed and rows[0]['ended_ms'] == closed
    assert rows[0]['causes']['wait_other'] > 15 * 60 * 1000, 'the minutes of the wait that earned the close'
    assert world.stops.read_text() == 'stop sid-5xxx\n', 'ended once'
    twin.kill()
    twin.wait()

    # The public relaunch path refuses before it launches anything, from the record the tick wrote.
    from plangraph.routing_errors import ParameterRefused
    from plangraph.spawner.sessions import Sessions
    from sessions_support import FakeRun, write_config
    config = world.root / 'sessions.json'
    write_config(config, json.dumps({'cwd_roots': [str(world.root)], 'models': ['m'], 'efforts': ['high'],
                                     'permission_modes': ['bypassPermissions'], 'mcp_configs': [], 'session_names': 'free'}))
    run = FakeRun()
    sessions = Sessions(state_root=world.spawn, config_path=str(config), run=run)
    with pytest.raises(ParameterRefused, match='BLOCKED.*sleep 402'):
        sessions.relaunch('writer-5')
    assert run.calls == [], 'the launcher and `claude stop` were never invoked'
