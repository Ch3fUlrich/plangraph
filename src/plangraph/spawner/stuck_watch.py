"""Conservative process evidence for the existing two-minute watchdog.

On 11-06 dell reviews run-713f07d9, run-511c0ebc, run-576c669b and
run-9aeb4877 wrote only a 55–80 byte banner until the 1500 s timeout.
Log silence alone cannot distinguish those from a buffered, busy seat.
Reuse reap.snapshot (also used by sample_tree) and kill_proven, never a new kill.
"""
import asyncio
import ipaddress
import json
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
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        table[int(path.name)] = dict(parent=int(fields[1]), tty=int(fields[4]),
                                    cpu=int(fields[11]) + int(fields[12]),
                                    start=int(fields[19]),
                                    cmd=(path / 'cmdline').read_bytes().replace(b'\0', b' ').decode())
    return table


def outbound(proc, members):
    inodes = set()
    for pid in members:
        for fd in (Path(proc) / str(pid) / 'fd').iterdir():
            target = fd.readlink().as_posix()
            if target.startswith('socket:['):
                inodes.add(target[8:-1])
    active = False
    for pid in members:  # per-process network namespace, not the watchdog's
        for name in ('tcp', 'tcp6'):
            lines = (Path(proc) / str(pid) / 'net' / name).read_text().splitlines()
            if not lines:
                raise ValueError('empty socket table')
            for line in lines[1:]:
                f = line.split()
                if f[3] != '01' or f[9] not in inodes:
                    continue
                raw = bytes.fromhex(f[2].split(':')[0])
                raw = b''.join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
                address = ipaddress.ip_address(raw)
                mapped = getattr(address, 'ipv4_mapped', None)
                active |= not (mapped or address).is_loopback
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


def check(state, runs, now, *, dry_run=False, proc='/proc', platform=None,
          cancel=None, kill=None):
    """One finding per tree. Unknown evidence resets the sampling interval."""
    if not (platform or sys.platform).startswith('linux'):
        return [dict(rule='stuck-watch-report', reason='process/socket evidence unavailable on this platform')]
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
        if not dry_run:
            atomic_write_private(path, '{}')
        return []
    records = {r['run_id']: r for r in runs}
    covered = set()
    roots = [p for p, row in table.items() if row['cmd'].split() and
             row['cmd'].split()[0].startswith('plangraph-job-')]
    for pid in roots:
        row = table[pid]
        run_id = row['cmd'].split()[0][len('plangraph-job-'):]
        record = records.get(run_id)
        try:
            members = reap.snapshot(pid, row['start'], proc_root=proc)
            if not members or any(p not in table or table[p]['start'] != s for p, s in members.items()):
                continue
            covered.update(members)
            safe = not any(table[p]['tty'] or '--service' in table[p]['cmd'].split() for p in members)
            old = previous.get(run_id, {})
            identity = {str(p): s for p, s in members.items()}
            # A stored title/start sample or the spawn record establishes ownership.
            proven = (record is not None and record.get('pid') == pid and
                      record.get('pid_start') == row['start']) or (
                          old.get('identity') == identity and old.get('root') == pid)
            cpu = sum(table[p]['cpu'] for p in members)
            network = outbound(proc, members)
            since = now
            if old.get('identity') == identity and old.get('cpu') == cpu and not network:
                since = min(now, old.get('since', now))
            samples[run_id] = dict(root=pid, identity=identity, cpu=cpu, since=since)
            finding = None
            if not safe or not proven:
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
                finding.update(run_id=run_id, pid=pid)
                if not dry_run and finding['rule'] == 'stuck-watch-idle':
                    finding['result'] = (cancel or _cancel)(state, record)
                elif not dry_run and finding['rule'] == 'stuck-watch-reap':
                    finding['survivors'] = (kill or _kill)(state, pid, row['start'], members)
                findings.append(finding)
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            samples.pop(run_id, None)
    for pid, row in table.items():
        words = row['cmd'].split()
        if pid not in covered and words and Path(words[0]).name == 'opencode' and 'serve' in words:
            key = 'process-' + str(pid)
            old = previous.get(key, {})
            since = old.get('since', now) if old.get('start') == row['start'] else now
            samples[key] = dict(start=row['start'], since=since)
            orphan = '--stdio' in words and (row['parent'] == 1 or row['parent'] not in table)
            if orphan or row['tty'] or '--service' in words or now - since >= 300:
                findings.append(dict(rule='stuck-watch-report', run_id=key, pid=pid,
                                     reason='unclaimed opencode serve; ownership unproven'))
    if not dry_run:
        atomic_write_private(path, json.dumps(samples))
    return findings
