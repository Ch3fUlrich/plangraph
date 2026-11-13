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
def inbox_lock(path, *, blocking=True):
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
            msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
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

    def item(self, key):
        from urllib.parse import urlencode
        token = Path(self.token_file).read_text(encoding='utf-8').strip()
        request = urllib.request.Request(
            self.url.rstrip('/') + '/v1/show?' + urlencode({'key': key, 'fmt': 'json'}),
            headers={'Authorization': 'Bearer ' + token})
        with urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=10) as response:
            return json.load(response)


def deliver_verdict(directory, record, keeper):
    """Never let optional verdict delivery interrupt completion persistence."""
    if record.get('role') != 'review' or (Path(directory) / 'verdict.delivered').exists():
        return
    try:
        _deliver_verdict(directory, record, keeper)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('verdict delivery deferred: %s', type(exc).__name__)


def _verdict_outcome(directory, status, reason):
    journal = directory / 'verdict.json'
    previous = json.loads(journal.read_text()) if journal.exists() else {}
    if previous.get('status') in ('skipped', 'delivered'):
        atomic_write_private(directory / 'verdict.delivered', 'terminal\n')
        return
    attempts = previous.get('attempts', 0) + 1
    if status == 'retry' and attempts >= 3:
        status, reason = 'skipped', 'retry limit reached: ' + reason
    atomic_write_private(journal, json.dumps(dict(status=status, reason=reason, attempts=attempts)) + '\n')
    if status in ('skipped', 'delivered'):
        atomic_write_private(directory / 'verdict.delivered', status + '\n')


def _deliver_verdict(directory, record, keeper):
    """Independent, locked verdict outbox; refusal is auditable, I/O is retried."""
    if record.get('role') != 'review':
        return
    import contextlib
    import io
    import sys
    tools = Path(__file__).resolve().parents[3] / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    from .item_key import valid_item
    directory = Path(directory)
    marker = directory / 'verdict.delivered'
    journal = directory / 'verdict.json'
    with inbox_lock(journal):
        if marker.exists():
            return
        def outcome(status, reason):
            _verdict_outcome(directory, status, reason)
        verifier_started = False
        try:
            import seat_verdict as seat
            if journal.exists() and json.loads(journal.read_text()).get('status') in ('skipped', 'delivered'):
                atomic_write_private(marker, 'terminal\n')
                return
            if journal.exists() and json.loads(journal.read_text()).get('status') == 'sending':
                outcome('skipped', 'ambiguous send, not retried')
                return
            key = record.get('item_key')
            if not valid_item(key):
                outcome('skipped', 'missing item key')
                return
            if (directory / 'evidence.json').exists():
                outcome('skipped', 'forward delivery requires record-derived evidence')
                return
            evidence, text = seat.evidence_from_record(directory)
            head = evidence.get('head_sha')
            if not isinstance(head, str) or len(head) != 40 or any(c not in '0123456789abcdefABCDEF' for c in head):
                raise ValueError('missing or invalid record head_sha')
            family = seat.model_family(evidence.get('served_model'))
            writers = record.get('given_writer_families') or []
            if not family or not writers or family in writers or any(w not in seat.KNOWN_FAMILIES for w in writers):
                outcome('skipped', 'unwitnessed family or invalid/overlapping writer families')
                return
            if seat.parse_verdict(text)[0] is None:
                outcome('skipped', 'no parseable VERDICT line')
                return
            token_dir = os.environ.get('PG_REVIEWER_TOKEN_DIR')
            if not keeper or not keeper.url or not token_dir:
                outcome('retry', 'keeper verdict transport not configured')
                return
            item = keeper.item(key)
            rows = item.get('evidence', [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) or
                    (row.get('verified') and row.get('kind') == 'commit+ci' and
                     not isinstance(row.get('ref'), dict)) for row in rows):
                raise ValueError('malformed item evidence')
            heads = [row.get('ref', {}).get('commit') for row in rows
                     if row.get('verified') and row.get('kind') == 'commit+ci']
            if item.get('key') != key or evidence['head_sha'] not in heads:
                outcome('skipped', 'missing or ambiguous item evidenced head')
                return
            if evidence['head_sha'] != heads[-1]:
                outcome('skipped', 'superseded head')
                return
            output = io.StringIO()
            atomic_write_private(journal, json.dumps(dict(status='sending',
                attempts=json.loads(journal.read_text()).get('attempts', 0) if journal.exists() else 0)) + '\n')
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                verifier_started = True
                rc = seat.main(['--key', key, '--seat-dir', str(directory),
                    '--writer-family', ','.join(writers), '--head', evidence['head_sha'],
                    '--token-dir', token_dir, '--url', keeper.url])
            if rc == 0:
                outcome('delivered', 'keeper acknowledged')
            else:
                outcome('retry' if rc == seat.EXIT_IO else 'skipped', 'seat verifier exit ' + str(rc))
        except ValueError as exc:
            outcome('skipped', str(exc))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('verdict delivery deferred: %s', type(exc).__name__)
            outcome('skipped' if verifier_started else 'retry', type(exc).__name__)


def completion_event(record, cwd):
    from .runner import redact
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=cwd,
                                       text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    rc = record.get('exit_code')
    return redact(dict(event_id=record['run_id'] + ':completion', run_id=record['run_id'],
        idempotency_key=record['run_id'] + ':' + record['status'],
        event='DONE' if rc == 0 else 'FAILED', rc=rc, head_sha=head,
        reason=record.get('reason'),
        **({'watchdog': record['watchdog']} if 'watchdog' in record else {}),
        parent_session=os.environ.get('PLANGRAPH_SPAWN_PARENT_SESSION'),
        item=record.get('item') or os.environ.get('PLANGRAPH_SPAWN_ITEM'),
        take_cmd=f'git cherry-pick {head}' if head else 'git log --oneline',
        discard_cmd=f'plangraph-spawn result {record["run_id"]} # discard: do not cherry-pick'))


def leg_refused_event(record, leg, reason):
    """One notify line per refused leg attempt (D-3605 a): run id, leg, reason."""
    from .runner import redact
    return redact(dict(event_id='%s:leg_refused:%s' % (record['run_id'], leg),
        idempotency_key='%s:leg_refused:%s' % (record['run_id'], leg),
        event='LEG_REFUSED', run_id=record['run_id'], leg=leg, reason=reason,
        parent_session=os.environ.get('PLANGRAPH_SPAWN_PARENT_SESSION'),
        item=record.get('item') or os.environ.get('PLANGRAPH_SPAWN_ITEM')))
