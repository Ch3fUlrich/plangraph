"""Deterministic stall detection; durable outbox consumed by routing/event delivery.

Session producers touch ``session-activity/<session>`` in the spawn state directory.
Run producers attach ``parent_session`` and ``item_key`` to their records. Unknown
activity is deliberately not interpreted as proof of idleness.
"""
from __future__ import annotations

import argparse
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from xml.sax.saxutils import escape

from .stall_probe import probe_run, probe_setting

_budget = ContextVar('probe_budget', default=None)

def tick(runs, claims, activity, *, now=None, deliver=None, state=None, dry_run=False, budget=None, hung=None, stuck=False):
    now = time.time() if now is None else now
    deliver = None if dry_run else deliver
    findings = []
    runs = list(runs)
    if state is not None and stuck:
        try:
            from .stuck_watch import check
            stuck_findings = check(state, runs, now, dry_run=dry_run)
            findings.extend(stuck_findings)
            if deliver:
                for finding in stuck_findings:
                    if not finding.get('diagnostic_only'):
                        deliver(dict(finding, kind='alert', recipient='routing'))
        except Exception as exc:
            findings.append(dict(rule='stuck-watch-error', reason=type(exc).__name__))
    for run in runs:
        if state is not None:
            finding = probe_run(state, run, now, dry_run=dry_run or (stuck and hung is None and run.get('status') == 'running') or (budget is not None and budget[0] <= 0), hung=hung)
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

def status(findings):
    return '\n'.join('! stall ' + json.dumps(f, sort_keys=True) for f in findings)

def read_runs(state, *, dry_run=False):
    from .item_key import valid_item
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
            if record.get('item_key') is None and valid_item(completion.get('item')):
                record['item_key'] = completion['item']
            if (path.parent / 'parent.delivered').exists():
                record['_completion_acknowledged'] = True
            runs.append(record)
        except (OSError, ValueError, UnicodeError) as exc:
            bad.append(dict(path=str(path), error=type(exc).__name__))
            if dry_run:
                runs.append(dict(run_id=path.parent.name))
            else:
                finding = probe_run(state, dict(run_id=path.parent.name), time.time())
                deliver_event(state, dict(finding, kind='completion'), budget=_budget.get())
    if bad and not dry_run:
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

def deliver_event(state, event, *, now=None, interval=None, budget=None):
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
    if budget is not None:
        if budget[0] <= 0:
            budget[1] += 1
            return
        budget[0] -= 1
    atomic_write_private(Path(state) / 'watchdog/outbox' / (key + '.json'), payload)
    atomic_write_private(stamp, json.dumps(dict(event, sent=now, count=count + 1)))

def flush_outbox(state, command, *, run=subprocess.run):
    """Send JSON on stdin to the host event bridge; remove only on success."""
    from .private_io import atomic_write_private
    health_path = Path(state) / 'watchdog/delivery.json'
    health = dict(failures=0, last_error=None) | (_read_state(state, health_path) or {})
    failed = False
    for path in sorted((Path(state) / 'watchdog/outbox').glob('*.json')):
        record = _read_state(state, path)
        if record is None:
            continue
        try:
            run(command, input=path.read_bytes(), check=True, timeout=20)
            path.unlink(missing_ok=True)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            failed = True
            health.update(failures=health['failures'] + 1, last_error=type(exc).__name__)
    if not failed:
        health.update(failures=0, last_error=None)
    health_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_private(health_path, json.dumps(health))
    return health

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
        config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'plangraph/watchdog.env'
        (destination / 'plangraph-remote-probe.service').write_text(
            '[Service]\nType=oneshot\nWorkingDirectory=' + str(root) + '\nEnvironment=PYTHONPATH=src\n'
            'Environment="PLANGRAPH_PROBE_ROOT=' + str(root) + '"\n'
            'EnvironmentFile=-"' + str(config) + '"\n'
            'ExecStart=' + python + ' -m plangraph.spawner.remote_probe\n')
        (destination / 'plangraph-remote-probe.timer').write_text(
            '[Timer]\nOnBootSec=2min\nOnUnitActiveSec=2min\n[Install]\nWantedBy=timers.target\n')
        run(['systemctl', '--user', 'daemon-reload'], check=True)
        configured = {}
        try:
            for line in config.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith(('#', ';')) or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                configured[key.strip()] = value
        except (OSError, UnicodeError):
            pass
        if not all(configured.get(k, '').strip()
                   for k in ('PLANGRAPH_PROBE_HOSTS', 'PLANGRAPH_PROBE_NOTIFY_CMD')):
            run(['systemctl', '--user', 'disable', '--now', 'plangraph-remote-probe.timer'], check=True)
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

def main(argv=None, *, hung_config=None):
    # Host integrations supply structured sampling and leg-scoped actions;
    # old callers remain disabled rather than guessing process ownership.
    from .hung_capture import HungBoundary
    hung = HungBoundary(**hung_config) if hung_config is not None else None
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
    if not args.dry_run:
        spawn_service(state=state).retry_completions()
    budget = [max(1, int(probe_setting('PLANGRAPH_PROBE_MAX_PER_TICK', 25, 25))), 0]
    deliver = lambda e: deliver_event(state, e, budget=budget)
    token = _budget.set(budget)
    try:
        runs = read_runs(state, dry_run=True) if args.dry_run else read_runs(state)
    finally:
        _budget.reset(token)
    findings = tick(runs, [], lambda s: read_activity(state, s),
                    deliver=deliver, state=state, dry_run=args.dry_run, budget=budget, hung=hung, stuck=True)
    error = warning = None
    try:
        findings += tick([r for r in runs if r.get('status') in ('running', 'queued')],
                         read_claims(), lambda s: read_activity(state, s),
                         deliver=deliver, dry_run=args.dry_run)
    except Exception as exc:
        if type(exc).__name__ in ('MissingKeeperURL', 'Unreachable'):
            warning = 'keeper skipped ' + type(exc).__name__  # keeper not configured/reachable: skip claim rule
        else:
            error = type(exc).__name__
    if args.dry_run:
        from collections import Counter
        for rule, count in sorted(Counter(f['rule'] for f in findings).items()):
            print(f'{rule}: {count}')
        print(status(findings[:5]))
        return 0
    if budget[1]:
        deliver_event(state, dict(rule='findings-capped', kind='completion',
                                 episode=int(time.time() // 120), held=budget[1]))
    command = os.environ.get('PLANGRAPH_WATCHDOG_DELIVERY')
    if command:
        try:
            health = flush_outbox(state, json.loads(command))
            if health and health['failures']:
                error = (error + '; ' if error else '') + 'delivery-' + health['last_error'] + ' failures=' + str(health['failures'])
        except Exception as exc:
            error = (error + '; ' if error else '') + 'delivery-' + type(exc).__name__
    pending = len(list((state / 'watchdog/outbox').glob('*.json')))
    if not command and (pending or findings):
        warning = (warning + '; ' if warning else '') + 'delivery-bridge-unconfigured'
    if command and pending:
        error = error or 'delivery-bridge-failed'
    text = status(findings)
    if not command:
        text += ('\n' if text else '') + f'outbox {pending}, no bridge'
    if warning:
        text += ('\n' if text else '') + '! stall watchdog warning ' + warning
    if error:
        text += ('\n' if text else '') + '! stall watchdog error ' + error
    from plangraph.spawner.private_io import atomic_write_private
    atomic_write_private(state / 'watchdog/status.txt', text + ('\n' if text else ''))
    print(text)
    return 1 if error else 0


if __name__ == '__main__':
    sys.exit(main())
