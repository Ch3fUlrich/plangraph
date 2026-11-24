"""The scheduled watchdog tick ends a writer's over-long background wait (round telemetry).

Drives ``stall_watchdog.main(['tick'])`` with a fake clock against the built Rust
``pg-spawnd round-watch``, a real command process and a transcript with an open wait.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from plangraph.spawner import stall_watchdog as w

T0 = 1_700_000_000.0
BINARY = Path(__file__).resolve().parents[1] / 'rust/target/debug/pg-spawnd'
pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not BINARY.exists(),
    reason='needs the Linux pg-spawnd binary: cargo build -p pg-spawnd --bin pg-spawnd')


def stamp(seconds):
    return datetime.datetime.fromtimestamp(T0 + seconds, datetime.UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def line(seconds, kind, **extra):
    return json.dumps(dict(type=kind, timestamp=stamp(seconds), **extra))


def tool(seconds, ident, command, **flags):
    return line(seconds, 'assistant', message=dict(id='m%d' % seconds, stop_reason='tool_use', content=[
        dict(type='tool_use', id=ident, name='Bash', input=dict(command=command, **flags))]))


def result(seconds, ident, task=None):
    extra = dict(toolUseResult=dict(backgroundTaskId=task)) if task else {}
    return line(seconds, 'user', message=dict(role='user', content=[dict(type='tool_result', tool_use_id=ident, content='ok')]), **extra)


def waiting(task, command, start):
    return [tool(start + 2, 'u-' + task, command, run_in_background=True), result(start + 2.5, 'u-' + task, task),
            line(start + 3, 'api-request'),
            tool(start + 4, 'w-' + task, 'until grep -q rc= /t/tasks/%s.output; do sleep 15; done' % task)]


def command_shell(session_id, command):
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=session_id)
    return subprocess.Popen(['/bin/sh', '-c', command + '; true'], env=env, start_new_session=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'pg-spawnd').symlink_to(BINARY)
    monkeypatch.setenv('PATH', f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path / 'spawn'))
    monkeypatch.setenv('ROUTING_STATE_DIR', str(tmp_path / 'routing'))
    monkeypatch.setattr(w, 'sweep_runs', lambda state: [])
    monkeypatch.setattr(w, 'read_activity', lambda state, session: None)
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    monkeypatch.setattr(w, 'flush_outbox', lambda state, command: None)
    clock = [T0]
    monkeypatch.setattr(w.time, 'time', lambda: clock[0])
    sessions = tmp_path / 'routing/launch/sessions'
    sessions.mkdir(parents=True)
    cwd = '/w/my_proj'
    (sessions / 'writer-5.json').write_text(json.dumps(dict(session_id='sid-5', cwd=cwd, session_name='writer-5')))
    transcript = tmp_path / 'home/.claude/projects/-w-my-proj/sid-5.jsonl'
    transcript.parent.mkdir(parents=True)

    class World:
        spawn = tmp_path / 'spawn'

        def tick(self, minutes, lines):
            transcript.write_text('\n'.join(lines) + '\n')
            clock[0] = T0 + minutes * 60
            assert w.main(['tick']) == 0
            status = self.spawn / 'watchdog/status.txt'
            return status.read_text() if status.exists() else ''

        def outbox(self):
            return sorted((self.spawn / 'watchdog/outbox').glob('round-wait-*.json'))

    return World()


def test_the_scheduled_tick_ends_the_recorded_command_once_then_blocks_the_second_wait(world):
    first = command_shell('sid-5', 'sleep 401')
    head = [line(0, 'user', promptId='p1', origin=dict(kind='human'), message=dict(role='user', content='go')),
            line(1, 'api-request')]
    one = head + waiting('bgA', 'sleep 401', 1)
    world.tick(1, one)
    assert 'round-wait' not in world.tick(14, one)
    assert first.poll() is None, 'under the cap nothing is stopped'
    status = world.tick(16, one)
    assert 'round-wait-stopped' in status and 'round-wait-notified' in status
    assert first.wait(timeout=20) is not None
    [event] = world.outbox()
    note = json.loads(event.read_text())
    assert note['recipient'] == 'writer-5' and 'sleep 401' in note['text']
    assert 'round-wait' not in world.tick(17, one), 'a later tick adds nothing'

    second = command_shell('sid-5', 'sleep 402')
    two = one + [line(17 * 60, 'user', origin=dict(kind='task-notification'),
                      message=dict(role='user', content='<task-notification><task-id>bgA</task-id></task-notification>')),
                 result(17 * 60 + 1, 'w-bgA')]
    two += [line(17 * 60 + 2, 'api-request')] + waiting('bgB', 'sleep 402', 17 * 60 + 2)
    world.tick(18, two)
    status = world.tick(34, two)
    assert 'round-blocked' in status and 'sleep 402' in status
    assert second.wait(timeout=20) is not None
    blocked = json.loads((world.spawn / 'round-watch/writer-5.json').read_text())['blocked']
    assert blocked['command'] == 'sleep 402'
    assert len(world.outbox()) == 3  # first notice, then one each for the writer and routing
    rows = [json.loads(row) for row in (world.spawn / 'round-telemetry/rows.jsonl').read_text().splitlines()]
    assert rows[0]['outcome'] == 'BLOCKED' and rows[0]['round_key'] == 'writer-5:p1'
