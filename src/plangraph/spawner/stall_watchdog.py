"""Deterministic stall detection; durable outbox consumed by routing/event delivery.

Session producers touch ``session-activity/<session>`` in the spawn state directory.
Run producers attach ``parent_session`` and ``item_key`` to their records. Unknown
activity is deliberately not interpreted as proof of idleness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from xml.sax.saxutils import escape


def tick(runs, claims, activity, *, now=None, deliver=None, state=None):
    now = time.time() if now is None else now
    findings = []
    runs = list(runs)
    for run in runs:
        if state is not None:
            finding = probe_run(state, run, now)
            if finding:
                findings.append(finding)
                if deliver:
                    deliver(dict(finding, kind='completion'))
        parent = run.get('parent_session')
        ended = run.get('ended')
        if not parent or ended is None or run.get('status') in ('running', 'queued'):
            continue
        if run.get('_completion_acknowledged'):
            if deliver:
                deliver(dict(rule='finished-child', run_id=run['run_id'], kind='clear'))
            continue
        try:
            last = activity(parent)
        except (ValueError, TypeError):
            findings.append(dict(rule='invalid-parent-session', run_id=run.get('run_id')))
            continue
        if last is None or now - ended <= 300 or now - last <= 300 or last > ended:
            if deliver and last is not None and last > ended:
                deliver(dict(rule='finished-child', run_id=run['run_id'], kind='clear'))
            continue
        finding = dict(rule='finished-child', run_id=run['run_id'],
                       parent_session=parent, ended=ended, exit_code=run.get('exit_code'))
        findings.append(finding)
        if deliver:
            deliver(dict(finding, kind='completion', recipient=parent))
            deliver(dict(finding, kind='alert', recipient='routing'))
    live = {r.get('item_key') for r in runs if r.get('status') in ('running', 'queued')}
    for claim in sorted(claims, key=lambda c: (not c['key'].startswith('plangraph:'), c['key'])):
        if claim.get('state') != 'claimed' or claim['key'] in live:
            continue
        session = claim.get('session_id') or claim.get('claimed_by')
        try:
            last = activity(session) if session else None
        except (ValueError, TypeError):
            findings.append(dict(rule='invalid-claim-session', item_key=claim['key']))
            continue
        if last is None or now - last <= 1800:
            continue
        finding = dict(rule='idle-claim', item_key=claim['key'],
                       owner=claim.get('claimed_by') or claim.get('owner'))
        findings.append(finding)
        if deliver:
            for recipient in dict.fromkeys([finding['owner'], 'routing']):
                if recipient:
                    deliver(dict(finding, kind='alert', recipient=recipient))
    return findings


def probe_run(state, record, now):
    """Independent host sampling; missing evidence never authorizes cancellation."""
    import asyncio
    import math
    from .private_io import atomic_write_private
    event = dict(rule='unknown')
    try:
        event.update(run_id=record.get('run_id'), leg=record.get('leg'), role=record.get('role'),
                     age=None, seconds_silent=None, last_line=None)
        directory = Path(state) / 'runs' / record['run_id']
        log = directory / 'live.log'
        try:
            with log.open(encoding='utf-8') as stream:
                stream.seek(max(0, log.stat().st_size - 4096))
                lines = stream.read().splitlines()
            event.update(seconds_silent=max(0, now - log.stat().st_mtime),
                         last_line=lines[-1][:200] if lines else '')
        except (OSError, UnicodeError):
            pass
        try:
            started = record['started']
            if math.isfinite(started):
                event['age'] = max(0, now - started)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        if record.get('status') is None and event['age'] is None:
            return event
        if record.get('status') == 'running':
            pid = record.get('pid')
            if not isinstance(pid, int) or pid <= 0 or os.name == 'nt':
                return event
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                event['rule'] = 'run-orphan'
                record = dict(record, status='failed', reason='orphaned', ended=now, exit_code=1)
                atomic_write_private(directory / 'record.json', json.dumps(record))
                return event
        silent = event['seconds_silent']
        if silent is None:
            return event
        limit = float((record.get('card') or {}).get('stall_min', 12)) * 60
        if not math.isfinite(limit) or limit <= 0:
            return event
        if record.get('status') == 'running':
            if silent < limit:
                return None
            event['rule'] = 'run-silent'
            event['episode'] = log.stat().st_mtime
            if silent >= 2 * limit:
                from .runner import spawn_service
                result = asyncio.run(spawn_service(state=state).cancel(record['run_id']))
                event['cancelled'] = result.get('status') == 'cancelled'
            return event
        message = str(record.get('final_message', '')) + ' ' + event['last_line']
        if record.get('role') == 'implement' and (record.get('reason') == 'writer_no_changes'
                or 'running in the background' in message.lower()):
            event['rule'] = 'run-ended-no-commit'
            return event
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return event


def status(findings):
    return '\n'.join('! stall ' + json.dumps(f, sort_keys=True) for f in findings)


def read_runs(state):
    from .private_io import atomic_write_private, read_text_retry
    runs, bad = [], []
    for path in sorted((Path(state) / 'runs').glob('run-*/record.json')):
        try:
            record = json.loads(read_text_retry(path))
            if not isinstance(record, dict) or not isinstance(record.get('run_id'), str):
                raise ValueError('invalid run record')
            completion = record.get('completion') if isinstance(record.get('completion'), dict) else {}
            if completion.get('parent_session') and not record.get('parent_session'):
                record['parent_session'] = completion['parent_session']
            if completion.get('item') and not record.get('item_key'):
                record['item_key'] = completion['item']
            if (path.parent / 'parent.delivered').exists():
                record['_completion_acknowledged'] = True
            runs.append(record)
        except (OSError, ValueError, UnicodeError) as exc:
            bad.append(dict(path=str(path), error=type(exc).__name__))
            finding = probe_run(state, dict(run_id=path.parent.name), time.time())
            deliver_event(state, dict(finding, kind='completion'))
    if bad:
        atomic_write_private(Path(state) / 'watchdog/bad-records.json', json.dumps(bad))
    return runs


def read_activity(state, session):
    if not session or Path(session).name != session or session in ('.', '..'):
        raise ValueError('invalid session id')
    try:
        return (Path(state) / 'session-activity' / session).stat().st_mtime
    except FileNotFoundError:
        return None


def read_claims(session=None):
    if session is None:
        from plangraph.keeper.cli_verbs import Session
        session = Session()
    return [c for c in session.call('GET', 'snapshot')['items'] if c.get('state') == 'claimed']


def _read_state(state, path, *, stamp=False):
    """Treat corrupt mailbox state as absent; retain evidence outside active queues."""
    from .private_io import atomic_write_private
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            raise ValueError('expected object')
        if stamp and (isinstance(record.get('sent'), bool) or
                      not isinstance(record.get('sent'), (int, float)) or
                      isinstance(record.get('count', 1), bool) or
                      not isinstance(record.get('count', 1), int) or
                      record.get('count', 1) < 1):
            raise ValueError('invalid alert stamp')
        return record
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        report = Path(state) / 'watchdog/bad-state.json'
        try:
            bad = json.loads(report.read_text(encoding='utf-8'))
            if not isinstance(bad, list):
                bad = []
        except (OSError, ValueError, UnicodeError):
            bad = []
        if not any(isinstance(r, dict) and r.get('path') == str(path) for r in bad):
            bad.append(dict(path=str(path), error=type(exc).__name__))
            atomic_write_private(report, json.dumps(bad))
        # Rename is atomic and does not require read permission on the bad file.
        try:
            path.replace(path.with_suffix('.json.corrupt'))
        except FileNotFoundError:
            pass
        return None


def deliver_event(state, event, *, now=None, interval=None):
    """Idempotent, private mailbox. Consumers acknowledge by removing the file.

    An unacknowledged completion survives ticks and process restarts. A still
    stalled parent is re-alerted only after the configured interval.
    """
    from plangraph.spawner.private_io import atomic_write_private
    if event.get('kind') == 'completion':
        event = dict(event, event_id=hashlib.sha256(json.dumps(
            [event.get('run_id'), event.get('rule'), event.get('cancelled'),
             event.get('episode')]).encode()).hexdigest())
    payload = json.dumps(event, sort_keys=True)
    if event.get('kind') == 'clear':
        for folder in ('alerts', 'outbox'):
            for path in (Path(state) / 'watchdog' / folder).glob('*.json'):
                record = _read_state(state, path, stamp=folder == 'alerts')
                if record is None:
                    continue
                if record.get('run_id') == event.get('run_id') and record.get('rule') == event.get('rule'):
                    path.unlink(missing_ok=True)
        return
    identity = {k: event.get(k) for k in ('rule', 'run_id', 'item_key', 'kind', 'recipient')}
    if event.get('kind') == 'completion':
        identity['event_id'] = event['event_id']
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    now = time.time() if now is None else now
    interval = float(os.environ.get('PLANGRAPH_WATCHDOG_REALERT_SECONDS', '1800')) if interval is None else interval
    stamp = Path(state) / 'watchdog/alerts' / (key + '.json')
    count = 0
    previous = _read_state(state, stamp, stamp=True)
    if previous is not None:
        if event.get('kind') == 'completion':
            return
        count = previous.get('count', 1)
        if event.get('rule') == 'finished-child':
            interval = min(300 * 2 ** min(count - 1, 4), 3600)
        if now - previous['sent'] < interval:
            return
    atomic_write_private(Path(state) / 'watchdog/outbox' / (key + '.json'), payload)
    atomic_write_private(stamp, json.dumps(dict(event, sent=now, count=count + 1)))


def flush_outbox(state, command, *, run=subprocess.run):
    """Send JSON on stdin to the host event bridge; remove only on success."""
    for path in sorted((Path(state) / 'watchdog/outbox').glob('*.json')):
        record = _read_state(state, path)
        if record is None:
            continue
        try:
            run(command, input=path.read_bytes(), check=True, timeout=20)
            path.unlink(missing_ok=True)
        except (OSError, subprocess.SubprocessError):
            continue


def install(host, destination, *, python=None, root=None, run=subprocess.run, disabled=False):
    python = python or sys.executable
    root = Path(root or Path(__file__).resolve().parents[3]).resolve()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    templates = root / 'deploy/watchdog'
    if host == 'linux':
        if any(char.isspace() for char in str(root)):
            raise ValueError('Linux watchdog root must not contain whitespace')
        for name in ('service', 'timer'):
            text = (templates / ('watchdog.' + name)).read_text()
            text = text.replace('@PYTHON@', python).replace('@ROOT@', str(root))
            (destination / ('plangraph-stall-watchdog.' + name)).write_text(text)
        (destination / 'plangraph-remote-probe.service').write_text(
            '[Service]\nType=oneshot\nWorkingDirectory=' + str(root) + '\nEnvironment=PYTHONPATH=src\n'
            'Environment="PLANGRAPH_PROBE_ROOT=' + str(root) + '"\n'
            'EnvironmentFile=-%h/.config/plangraph/watchdog.env\n'
            'ExecStart=' + python + ' -m plangraph.spawner.remote_probe\n')
        (destination / 'plangraph-remote-probe.timer').write_text(
            '[Timer]\nOnBootSec=2min\nOnUnitActiveSec=2min\n[Install]\nWantedBy=timers.target\n')
        run(['systemctl', '--user', 'daemon-reload'], check=True)
        run(['systemctl', '--user', 'enable', '--now', 'plangraph-stall-watchdog.timer'], check=True)
    elif host == 'windows':
        text = (templates / 'watchdog.xml').read_text()
        text = text.replace('@PYTHON@', escape(python)).replace('@ROOT@', escape(str(root)))
        task = destination / 'plangraph-stall-watchdog.xml'
        task.write_bytes(b'\xff\xfe' + text.encode('utf-16-le'))
        run(['schtasks', '/Create', '/TN', 'Plangraph Stall Watchdog', '/XML', str(task), '/F'], check=True)
        if disabled:
            run(['schtasks', '/Change', '/TN', 'Plangraph Stall Watchdog', '/DISABLE'], check=True)
    else:
        raise ValueError('unsupported watchdog host')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('tick', 'status', 'install'))
    parser.add_argument('--host', choices=('linux', 'windows'), default=sys.platform if sys.platform == 'linux' else 'windows')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--disabled', action='store_true', help='Create the Windows task disabled')
    args = parser.parse_args(argv)
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE',
                 Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
    if args.command == 'install':
        destination = (Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd/user'
                       if args.host == 'linux' else state / 'watchdog/task')
        if args.dry_run:
            print(f'Would install {args.host} stall watchdog every 2 minutes in {destination}')
            return
        install(args.host, destination, disabled=args.disabled)
        return
    if args.command == 'status':
        path = state / 'watchdog/status.txt'
        print(path.read_text() if path.exists() else '', end='')
        return
    # Completion delivery must not be blocked by an unavailable keeper.
    from .runner import spawn_service
    spawn_service(state=state).retry_completions()
    runs = read_runs(state)
    findings = tick(runs, [], lambda s: read_activity(state, s),
                    deliver=lambda e: deliver_event(state, e), state=state)
    error = warning = None
    try:
        findings += tick([r for r in runs if r.get('status') in ('running', 'queued')],
                         read_claims(), lambda s: read_activity(state, s),
                         deliver=lambda e: deliver_event(state, e))
    except Exception as exc:
        if type(exc).__name__ in ('MissingKeeperURL', 'Unreachable'):
            warning = type(exc).__name__  # keeper not configured/reachable: skip claim rule
        else:
            error = type(exc).__name__
    command = os.environ.get('PLANGRAPH_WATCHDOG_DELIVERY')
    if command:
        try:
            flush_outbox(state, json.loads(command))
        except Exception as exc:
            error = 'delivery-' + type(exc).__name__
    elif any(f['rule'] != 'unknown' for f in findings):
        error = error or 'delivery-bridge-unconfigured'
    text = status(findings)
    if warning:
        text += ('\n' if text else '') + '! stall watchdog warning keeper skipped ' + warning
    if error:
        text += ('\n' if text else '') + '! stall watchdog error ' + error
    from plangraph.spawner.private_io import atomic_write_private
    atomic_write_private(state / 'watchdog/status.txt', text + ('\n' if text else ''))
    print(text)
    return 1 if error else 0


if __name__ == '__main__':
    sys.exit(main())
