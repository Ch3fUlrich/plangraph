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


def tick(runs, claims, activity, *, now=None, deliver=None):
    now = time.time() if now is None else now
    findings = []
    runs = list(runs)
    for run in runs:
        parent = run.get('parent_session')
        ended = run.get('ended')
        if not parent or ended is None or run.get('status') in ('running', 'queued'):
            continue
        last = activity(parent)
        if last is None or now - ended <= 300 or now - last <= 300 or last > ended:
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
        last = activity(session) if session else None
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


def read_runs(state):
    return [json.loads(p.read_text(encoding='utf-8'))
            for p in sorted((Path(state) / 'runs').glob('run-*/record.json'))]


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


def deliver_event(state, event):
    """Idempotent, private mailbox. Consumers acknowledge by removing the file.

    An unacknowledged completion survives ticks and process restarts. A still
    stalled parent gets another delivery after acknowledgement on the next tick.
    """
    from plangraph.spawner.private_io import atomic_write_private
    payload = json.dumps(event, sort_keys=True)
    key = hashlib.sha256(payload.encode()).hexdigest()
    atomic_write_private(Path(state) / 'watchdog/outbox' / (key + '.json'), payload)


def flush_outbox(state, command, *, run=subprocess.run):
    """Send JSON on stdin to the host event bridge; remove only on success."""
    for path in sorted((Path(state) / 'watchdog/outbox').glob('*.json')):
        run(command, input=path.read_text(encoding='utf-8'), text=True,
            check=True, timeout=20)
        path.unlink(missing_ok=True)


def install(host, destination, *, python=None, root=None, run=subprocess.run):
    python = python or sys.executable
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    templates = root / 'deploy/watchdog'
    if host == 'linux':
        for name in ('service', 'timer'):
            text = (templates / ('watchdog.' + name)).read_text()
            text = text.replace('@PYTHON@', python).replace('@ROOT@', str(root))
            (destination / ('plangraph-stall-watchdog.' + name)).write_text(text)
        run(['systemctl', '--user', 'daemon-reload'], check=True)
        run(['systemctl', '--user', 'enable', '--now', 'plangraph-stall-watchdog.timer'], check=True)
    elif host == 'windows':
        text = (templates / 'watchdog.xml').read_text()
        text = text.replace('@PYTHON@', escape(python)).replace('@ROOT@', escape(str(root)))
        task = destination / 'plangraph-stall-watchdog.xml'
        task.write_text(text, encoding='utf-8')
        run(['schtasks', '/Create', '/TN', 'Plangraph Stall Watchdog', '/XML', str(task), '/F'], check=True)
    else:
        raise ValueError('unsupported watchdog host')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('tick', 'status', 'install'))
    parser.add_argument('--host', choices=('linux', 'windows'), default=sys.platform if sys.platform == 'linux' else 'windows')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE',
                 Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
    if args.command == 'install':
        destination = (Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd/user'
                       if args.host == 'linux' else state / 'watchdog/task')
        if args.dry_run:
            print(f'Would install {args.host} stall watchdog every 2 minutes in {destination}')
            return
        install(args.host, destination)
        return
    if args.command == 'status':
        path = state / 'watchdog/status.txt'
        print(path.read_text() if path.exists() else '', end='')
        return
    # Completion delivery must not be blocked by an unavailable keeper.
    runs = read_runs(state)
    findings = tick(runs, [], lambda s: read_activity(state, s),
                    deliver=lambda e: deliver_event(state, e))
    error = None
    try:
        findings += tick([r for r in runs if r.get('status') in ('running', 'queued')],
                         read_claims(), lambda s: read_activity(state, s),
                         deliver=lambda e: deliver_event(state, e))
    except Exception as exc:
        error = type(exc).__name__
    command = os.environ.get('PLANGRAPH_WATCHDOG_DELIVERY')
    if command:
        try:
            flush_outbox(state, json.loads(command))
        except Exception as exc:
            error = 'delivery-' + type(exc).__name__
    elif findings:
        error = error or 'delivery-bridge-unconfigured'
    text = status(findings)
    if error:
        text += ('\n' if text else '') + '! stall watchdog error ' + error
    from plangraph.spawner.private_io import atomic_write_private
    atomic_write_private(state / 'watchdog/status.txt', text + ('\n' if text else ''))
    print(text)
    return 1 if error else 0


if __name__ == '__main__':
    sys.exit(main())
