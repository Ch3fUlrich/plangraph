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
import urllib.error
import urllib.request
from contextlib import contextmanager

from .private_io import atomic_write_private


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx answer is a failed delivery, not an ack.

    urlopen would otherwise turn an unacknowledged POST into a GET that ends
    on some 200 page and record keeper.delivered for an answer the keeper
    never gave. Raising here hands the 3xx to the caller as a failure.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code,
            'keeper redirected: not an acknowledgement', headers, fp)


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


class KeeperNotConfigured(ValueError):
    """Raised when live keeper delivery is unconfigured."""


class KeeperSink:
    """Deliver through the keeper's idempotent run_recorded item-event API.

    The keeper deduplicates (item, run_id, phase), including after a local
    marker-write failure. Requires a spawner-role token; never logs its value.
    """
    def __init__(self, url=None, token_file=None):
        self.url = os.environ.get('PG_URL', '') if url is None else url
        self.token_file = os.environ.get('PG_TOKEN_FILE', '') if token_file is None else token_file

    def deliver(self, recipient, event):
        if not self.url or not self.token_file:
            raise KeeperNotConfigured('live keeper delivery requires PG_URL and PG_TOKEN_FILE (spawner identity)')
        if not event.get('item'):
            raise ValueError('live keeper delivery requires PLANGRAPH_SPAWN_ITEM or record item')
        token = Path(self.token_file).read_text(encoding='utf-8').strip()
        payload = {'key': event['item'], 'run': {
            'run_id': event['run_id'], 'phase': 'finished', 'rc': event['rc']}}
        request = urllib.request.Request(self.url.rstrip('/') + '/v1/record_run',
            data=json.dumps(payload).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError('keeper did not acknowledge run completion')
            response.read()


def keeper_item_reader(url=None, token_file=None):
    """D-2739 gates: ``key -> keeper show row``, or None when no keeper is configured.

    A configured keeper that cannot answer refuses the spawn (fail closed); a key
    the keeper does not hold reads as None (gates skipped, noted on the record).
    The token is read per call and never logged.
    """
    url = os.environ.get('PG_URL', '') if url is None else url
    token_file = os.environ.get('PG_TOKEN_FILE', '') if token_file is None else token_file
    if not url or not token_file:
        return None

    def read(key):
        from plangraph.routing_errors import SpawnRefused
        import urllib.parse
        try:
            token = Path(token_file).read_text(encoding='utf-8').strip()
            request = urllib.request.Request(
                url.rstrip('/') + '/v1/show?' + urllib.parse.urlencode({'key': key, 'fmt': 'json'}),
                headers={'Authorization': 'Bearer ' + token})
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=10) as response:
                row = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise SpawnRefused(f'efficiency gates: keeper show {key} failed (HTTP {exc.code})') from None
        except Exception as exc:  # noqa: BLE001 - any failure fails the gate closed
            raise SpawnRefused(f'efficiency gates: keeper show {key} failed ({type(exc).__name__})') from None
        if not isinstance(row, dict):
            raise SpawnRefused(f'efficiency gates: keeper show {key} returned no item')
        return row if row.get('key') else None
    return read


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
