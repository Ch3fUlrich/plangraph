"""Durable completion inboxes and optional fleet transport adapters.

Sinks implement deliver(recipient, event). Consumers deduplicate on event_id.
Keeper delivery is an item-event outbox, not authorization to close an item.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
from contextlib import contextmanager

from .private_io import atomic_write_private


@contextmanager
def inbox_lock(path):
    """Serialize independent supervisors sharing an inbox, on both host types."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)


class FileInbox:
    def __init__(self, state):
        self.state = Path(state)

    def deliver(self, recipient, event):
        # Atomic replacement makes replay after a crash idempotent. Saves occur
        # synchronously in the supervisor; recipients have independent journals.
        path = self.state / 'inbox' / (recipient + '.jsonl')
        with inbox_lock(path):
            previous = path.read_text(encoding='utf-8') if path.exists() else ''
            if any(json.loads(line).get('idempotency_key', json.loads(line)['event_id']) ==
                   event.get('idempotency_key', event['event_id']) for line in previous.splitlines()):
                return
            atomic_write_private(path, previous + json.dumps(event) + '\n')


class CommandSink:
    def __init__(self, command):
        self.command = shlex.split(command)

    def deliver(self, recipient, event):
        subprocess.run(self.command + [recipient], input=json.dumps(event) + '\n',
                       text=True, check=True, timeout=10, stdout=subprocess.DEVNULL)


def completion_event(record, cwd):
    from .runner import redact
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=cwd,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    rc = record.get('exit_code')
    return redact(dict(event_id=record['run_id'] + ':completion', run_id=record['run_id'],
        idempotency_key=record['run_id'] + ':' + record['status'],
        event='DONE' if rc == 0 else 'FAILED', rc=rc, head_sha=head,
        parent_session=os.environ.get('PLANGRAPH_SPAWN_PARENT_SESSION'),
        item=record.get('item') or os.environ.get('PLANGRAPH_SPAWN_ITEM'),
        take_cmd=f'git cherry-pick {head}' if head else 'git log --oneline',
        discard_cmd=f'plangraph-spawn result {record["run_id"]} # discard: do not cherry-pick'))
