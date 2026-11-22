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
                       text=True, encoding="utf-8", errors="replace", check=True, timeout=10, stdout=subprocess.DEVNULL)


class KeeperNotConfigured(ValueError):
    """Raised when live keeper delivery is unconfigured."""


class KeeperRefused(Exception):
    """The keeper answered a delivery with a 4xx refusal (``status``)."""

    def __init__(self, status, detail=''):
        super().__init__(f'keeper refused delivery: HTTP {status} {detail}'.strip())
        self.status = status


# Refusals that say nothing about the delivery itself and may clear on their own.
_TRANSIENT_REFUSALS = frozenset({401, 403, 408, 425, 429})


def refusal_is_final(directory, refusal):
    """Whether a refused delivery is never sent again.

    A conflict (HTTP 409: the item no longer takes this record, e.g. it is done)
    is final at once; any other 4xx that is not transient is final when the same
    status repeats for this run (the first one is remembered in ``keeper.refused``).
    """
    if refusal.status == 409:
        return True
    if refusal.status in _TRANSIENT_REFUSALS:
        return False
    seen = Path(directory) / 'keeper.refused'
    if seen.exists() and seen.read_text(encoding='utf-8').strip() == str(refusal.status):
        return True
    atomic_write_private(seen, f'{refusal.status}\n')
    return False


def settle_refusal(marker, recipient, refusal):
    """Mark a final refusal as delivered-as-refused (never resent); log a retryable one."""
    import logging
    log = logging.getLogger(__name__)
    if refusal_is_final(Path(marker).parent, refusal):
        atomic_write_private(marker, f'refused: HTTP {refusal.status}\n')
        log.warning('completion delivery to %s refused (HTTP %s): not retried', recipient, refusal.status)
    else:
        log.warning('completion delivery to %s refused (HTTP %s): will retry', recipient, refusal.status)


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
        try:
            with opener.open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError('keeper did not acknowledge run completion')
                response.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise KeeperRefused(exc.code) from None
            raise

    def item(self, key):
        from urllib.parse import urlencode
        token = Path(self.token_file).read_text(encoding='utf-8').strip()
        request = urllib.request.Request(
            self.url.rstrip('/') + '/v1/show?' + urlencode({'key': key, 'fmt': 'json'}),
            headers={'Authorization': 'Bearer ' + token})
        with urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=10) as response:
            return json.load(response)


    def admit_seat_verdict(self, record, cwd):
        """Pin the review target and round before the seat receives its prompt."""
        from .prompt_input import checkout_env

        key = record['item_key']
        request = record.setdefault('request', {})
        target = request.get('review_head') or 'HEAD'
        head = subprocess.check_output(
            ['git', 'rev-parse', '--verify', '--end-of-options', target + '^{commit}'],
            cwd=cwd, env=checkout_env(), timeout=10, text=True,
            encoding="utf-8", errors="replace").strip()
        item = self.item(key)
        binding = dict(key=key, epoch=item['epoch'], round=item['round'],
                       head=head, seat_run=record['run_id'])
        token = Path(self.token_file).read_text(encoding='utf-8').strip()
        message = urllib.request.Request(self.url.rstrip('/') + '/v1/admit_seat_verdict',
            data=json.dumps(binding).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(_NoRedirectHandler()).open(message, timeout=10) as response:
            if json.load(response) != binding:
                raise ValueError('keeper did not acknowledge seat admission')
        request['verdict_binding'] = binding


