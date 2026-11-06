"""Conservative process evidence for the existing two-minute watchdog.

On 11-06 dell reviews run-713f07d9, run-511c0ebc, run-576c669b and
run-9aeb4877 wrote only a 55–80 byte banner until the 1500 s timeout.
Log silence alone cannot distinguish those from a buffered, busy seat.
Reuse reap.snapshot (also used by sample_tree) and kill_proven, never a new kill.
"""
import asyncio
import ipaddress
import json
import os
import socket
from pathlib import Path
import sys

from . import reap
from .private_io import atomic_write_private


def processes(proc):
    """A partial/unreadable proc view is not evidence of inactivity."""
    table = {}
    for path in Path(proc).iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            table[int(path.name)] = dict(parent=int(fields[1]), tty=int(fields[4]),
                                        cpu=int(fields[11]) + int(fields[12]),
                                        start=int(fields[19]),
                                        cmd=(path / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace'))
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
    return table


def outbound(proc, members):
    """None means incomplete evidence, never evidence of inactivity."""
    inodes = set()
    for pid in members:
        try:
            for fd in (Path(proc) / str(pid) / 'fd').iterdir():
                target = fd.readlink().as_posix()
                if target.startswith('socket:['):
                    inodes.add(target[8:-1])
        except OSError:
            return None
    try:
        return _net_active(proc, members, inodes)
    except (OSError, ValueError, IndexError):
        return None


def _net_active(proc, members, inodes):
    active = False
    for pid in members:  # per-process network namespace, not the watchdog's
        for name in ('tcp', 'tcp6', 'udp', 'udp6'):
            lines = (Path(proc) / str(pid) / 'net' / name).read_text().splitlines()
            if not lines:
                raise ValueError('empty socket table')
            for line in lines[1:]:
                f = line.split()
                # Include connecting/closing TCP and connected UDP, not listeners
                # or unconnected sockets with an unspecified remote endpoint.
                if f[3] == '0A' or f[9] not in inodes:
                    continue
                raw = bytes.fromhex(f[2].split(':')[0])
                raw = b''.join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
                address = ipaddress.ip_address(raw)
                mapped = getattr(address, 'ipv4_mapped', None)
                remote = mapped or address
                active |= not (remote.is_loopback or remote.is_unspecified)
    return active


def _cancel(state, record):
    from .runner import spawn_service
    service = spawn_service(state=state)
    result = asyncio.run(service.cancel(record['run_id']))
    result['reason'] = 'stuck_watch_idle_tree'
    service._save(result)
    return result['status']


def _kill(state, pid, start, members):
    from .runner import spawn_service
    return asyncio.run(reap.kill_proven(spawn_service(state=state).runner, pid,
                                      start, plat=None, seen=members))


def _alert_due(sample, old, finding, now):
    """Persist rate limits with the process identity, including action attempts."""
    condition = [finding['rule'], finding['reason']]
    interval = float(os.environ.get('PLANGRAPH_WATCHDOG_REALERT_SECONDS', '1800'))
    if old.get('condition') == condition:
        for key in ('condition', 'last_alert', 'last_action'):
            if key in old:
                sample[key] = old[key]
        if now - old.get('last_alert', now) < interval:
            return False
    sample.update(condition=condition, last_alert=now)
    return True


def check(state, runs, now, *, dry_run=False, proc='/proc', platform=None,
          cancel=None, kill=None):
    """One finding per tree. Unknown evidence resets the sampling interval."""
    host = os.environ.get('PLANGRAPH_HOST') or socket.gethostname()
    if not (platform or sys.platform).startswith('linux'):
        return [dict(rule='stuck-watch-report', diagnostic_only=True, host=host,
                     reason='process/socket evidence unavailable on this platform')]
    state = Path(state)
    path = state / 'watchdog/stuck-samples.json'
    try:
        previous = json.loads(path.read_text())
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    samples, findings = {}, []
    try:
        table = processes(proc)
    except (OSError, ValueError, IndexError, UnicodeError):
        return []
    records = {r['run_id']: r for r in runs}
    covered = set()
    roots = [p for p, row in table.items() if row['cmd'].split() and
              Path(row['cmd'].split()[0]).name.startswith('plangraph-job-')]
    for pid in roots:
        row = table[pid]
        run_id = Path(row['cmd'].split()[0]).name[len('plangraph-job-'):]
        record = records.get(run_id)
        try:
            members = reap.snapshot(pid, row['start'], proc_root=proc)
            if not members or any(p not in table or table[p]['start'] != s for p, s in members.items()):
                continue
            covered.update(members)
            safe = not any(table[p]['tty'] or '--service' in table[p]['cmd'].split() for p in members)
            old = previous.get(run_id, {})
            if old.get('host') != host:
                old = {}
            identity = {str(p): s for p, s in members.items()}
            # Only previously proven, same-host evidence can preserve ownership.
            proven = (record is not None and record.get('pid') == pid and
                      record.get('pid_start') == row['start']) or (
                           old.get('proven') is True and old.get('identity') == identity
                           and old.get('root') == pid and record is None)
            cpu = sum(table[p]['cpu'] for p in members)
            network = outbound(proc, members)
            since = now
            if old.get('identity') == identity and old.get('cpu') == cpu and network is False:
                since = min(now, old.get('since', now))
            samples[run_id] = dict(root=pid, identity=identity, cpu=cpu, since=since,
                                   proven=bool(proven), host=host)
            finding = None
            if network is None:
                finding = dict(rule='stuck-watch-report', reason='process/socket evidence unavailable')
            elif not safe or not proven:
                finding = dict(rule='stuck-watch-report', reason='TTY/service or unproven job tree')
            elif record is None:
                # Do not confuse an unreadable/corrupt record with an absent one.
                record_path = state / 'runs' / run_id / 'record.json'
                if record_path.exists():
                    continue
                finding = dict(rule='stuck-watch-reap', reason='missing record')
            elif record.get('status') in ('cancelled', 'failed', 'completed', 'succeeded', 'needs_input'):
                finding = dict(rule='stuck-watch-reap', reason='terminal record')
            elif record.get('status') == 'running' and now - since >= 300:
                finding = dict(rule='stuck-watch-idle', reason='CPU unchanged and no outbound socket for 300 s')
            elif record.get('status') == 'running' and since < now and not network:
                from .run_watchdog_limits import thresholds
                limit = thresholds(record.get('card'), role=record.get('role'))['max_s']
                if now - record.get('started', now) > limit:
                    finding = dict(rule='stuck-watch-report', reason='past role limit without activity')
            if finding is None and any(table[p]['cmd'].split() and Path(table[p]['cmd'].split()[0]).name == 'opencode' and
                    'serve' in table[p]['cmd'].split() and '--stdio' in table[p]['cmd'].split()
                    and (table[p]['parent'] == 1 or table[p]['parent'] not in table) for p in members):
                finding = dict(rule='stuck-watch-reap', reason='proven orphan stdio')
            if finding:
                finding.update(run_id=run_id, pid=pid, host=host)
                alert_old = old if old.get('identity') == identity else {}
                if not _alert_due(samples[run_id], alert_old, finding, now):
                    continue
                if not dry_run and finding['rule'] == 'stuck-watch-idle':
                    samples[run_id]['last_action'] = now
                    finding['result'] = (cancel or _cancel)(state, record)
                elif not dry_run and finding['rule'] == 'stuck-watch-reap':
                    samples[run_id]['last_action'] = now
                    finding['survivors'] = (kill or _kill)(state, pid, row['start'], members)
                findings.append(finding)
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            samples.pop(run_id, None)
    for pid, row in table.items():
        words = row['cmd'].split()
        if pid not in covered and words and Path(words[0]).name == 'opencode' and 'serve' in words:
            key = 'process-' + str(pid)
            old = previous.get(key, {})
            if old.get('host') != host:
                old = {}
            since = old.get('since', now) if old.get('start') == row['start'] else now
            samples[key] = dict(start=row['start'], since=since, host=host)
            orphan = '--stdio' in words and (row['parent'] == 1 or row['parent'] not in table)
            if orphan or row['tty'] or '--service' in words or now - since >= 300:
                finding = dict(rule='stuck-watch-report', run_id=key, pid=pid, host=host,
                               reason='unclaimed opencode serve; ownership unproven')
                alert_old = old if old.get('start') == row['start'] else {}
                if _alert_due(samples[key], alert_old, finding, now):
                    findings.append(finding)
    if not dry_run:
        atomic_write_private(path, json.dumps(samples))
    return findings
