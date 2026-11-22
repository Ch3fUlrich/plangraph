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


def refusal_is_final(directory, refusal, *, journal_name='keeper.refused'):
    """Whether a refused delivery is never sent again.

    A conflict (HTTP 409: the item no longer takes this record, e.g. it is done)
    is final at once; any other 4xx that is not transient is final when the same
    status repeats for this delivery (the first one is remembered in its journal).
    """
    if refusal.status == 409:
        return True
    if refusal.status in _TRANSIENT_REFUSALS:
        return False
    seen = Path(directory) / journal_name
    if seen.exists() and seen.read_text(encoding='utf-8').strip() == str(refusal.status):
        return True
    atomic_write_private(seen, f'{refusal.status}\n')
    return False


def settle_refusal(marker, recipient, refusal, *, final_statuses=()):
    """Mark a final refusal as delivered-as-refused (never resent); log a retryable one."""
    import logging
    log = logging.getLogger(__name__)
    marker = Path(marker)
    if refusal.status in final_statuses or refusal_is_final(
            marker.parent, refusal, journal_name=marker.with_suffix('.refused').name):
        atomic_write_private(marker, f'refused: HTTP {refusal.status}\n')
        log.warning('delivery to %s refused (HTTP %s): not retried', recipient, refusal.status)
    else:
        log.warning('delivery to %s refused (HTTP %s): will retry', recipient, refusal.status)


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
        self._post("record_run", payload, token)

    def _post(self, verb, payload, token):
        request = urllib.request.Request(self.url.rstrip('/') + '/v1/' + verb,
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

    def spawn_attempt(self, attempt):
        token = Path(self.token_file).read_text(encoding="utf-8").strip()
        self._post("record_spawn_attempt", attempt, token)

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


def retry_spawn_attempts(state, runs, *, keeper=None, limit=25):
    """Deliver terminal snapshots idempotently and retain undelivered records."""
    import re
    from plangraph.fleet_snapshot import _projection_binary

    keeper = keeper if keeper is not None else KeeperSink()
    if not keeper.url or not keeper.token_file:
        return []
    pending = []
    for run in runs:
        run_id = run.get("run_id", "")
        if (run.get("attempt_capture") != 1 or not run.get("ended")
                or run.get("status") in ("queued", "running", "queue_claimed")
                or not re.fullmatch(r"run-[A-Za-z0-9-]+", run_id)):
            continue
        marker = Path(state) / "runs" / run_id / "attempt.delivered"
        if not marker.exists():
            pending.append((run, marker))
    findings = []
    for run, marker in sorted(pending, key=lambda pair: pair[0]["ended"])[:limit]:
        try:
            result = subprocess.run([_projection_binary(), "project-spawn-attempt"],
                                    input=json.dumps(run, allow_nan=False), text=True,
                                    encoding="utf-8", errors="replace", capture_output=True, timeout=5, check=True)
            attempt = json.loads(result.stdout)
            if attempt is not None:
                try:
                    keeper.spawn_attempt(attempt)
                except KeeperRefused as refusal:
                    settle_refusal(marker, f"keeper (attempt {run['run_id']})",
                                   refusal, final_statuses=(400, 422))
                    raise
            atomic_write_private(marker, "delivered\n" if attempt is not None else "not_launched\n")
        except (OSError, ValueError, RuntimeError, KeeperRefused, subprocess.SubprocessError) as exc:
            findings.append(dict(rule="spawn-attempt-delivery", run_id=run["run_id"],
                                 reason=type(exc).__name__))
            if isinstance(exc, (OSError, subprocess.TimeoutExpired)):
                break
    return findings


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
    if status == 'retry' and attempts >= 3 and not (directory / 'verdict.payload.json').exists():
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
            saved = (directory / 'verdict.payload.json').is_file()
            if journal.exists() and json.loads(journal.read_text()).get('status') == 'sending' and not saved:
                seat.mark_verdict_recovery(directory, 'legacy sending entry has no bound payload')
                outcome('skipped', 'ambiguous send, not retried')
                return
            key = record.get('item_key')
            if not valid_item(key):
                outcome('skipped', 'missing item key')
                return
            if (directory / 'evidence.json').exists() and not record.get('head_sha'):
                outcome('skipped', 'forward delivery requires record-derived evidence')
                return
            if saved:
                token_dir = os.environ.get('PG_REVIEWER_TOKEN_DIR')
                if not keeper or not keeper.url or not token_dir:
                    outcome('retry', 'keeper verdict transport not configured')
                    return
                rc = seat.send_saved_verdict(directory, key, keeper.url, Path(token_dir))
                outcome('delivered' if rc == 0 else 'retry' if rc == seat.EXIT_IO else 'skipped',
                        'stored verdict delivery exit ' + str(rc))
                return
            evidence, text = seat.evidence_from_record(directory)
            head = evidence.get('head_sha')
            if not isinstance(head, str) or len(head) != 40 or any(c not in '0123456789abcdefABCDEF' for c in head):
                raise ValueError('missing or invalid record head_sha')
            family = seat.model_family(evidence.get('served_model'))
            from plangraph.review.family import writer_key
            writers = sorted({writer_key(w) for w in record.get('given_writer_families') or []})
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
            outcome('retry' if not verifier_started or (directory / 'verdict.payload.json').is_file() else 'skipped', type(exc).__name__)


def completion_event(record, cwd):
    from .runner import redact
    from .prompt_input import checkout_env
    head = record.get('head_sha') if record.get('review_checkout') else None
    if head is None:
        try:
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=cwd,
                                           text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
                                           env=checkout_env(), timeout=10).strip()
        except (OSError, subprocess.SubprocessError):
            pass
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
