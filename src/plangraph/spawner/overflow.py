"""RAM-based local admission and opt-in public-repository SSH placement."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from typing import NamedTuple

from plangraph.keeper.credential_guard import mask
from plangraph.fleet_sync.genconfig import config_dir

from . import cli
from .private_io import atomic_write_private


class Decision(NamedTuple):
    host: str | None
    reason: str


class RemoteRefusal(ValueError):
    """Safe, value-free remote failure reason."""


def failure_reason(exc):
    """Describe the failure without exposing exception values or private paths."""
    if isinstance(exc, OSError):
        return 'remote spawn OS operation failed: ' + (os.strerror(exc.errno) if exc.errno else 'I/O error')
    if isinstance(exc, KeyError):
        return 'remote spawn configuration missing a required field'
    if isinstance(exc, TypeError):
        return 'remote spawn input has an invalid type'
    return 'remote spawn input or receipt has an invalid value'


def registry_caps():
    """Read the same registry location and JSON format as fleet-sync."""
    try:
        registry = json.loads((config_dir() / 'hosts.json').read_text())
        if not isinstance(registry['hosts'], list):
            raise ValueError('hosts must be a list')
        return registry
    except (OSError, ValueError, KeyError, TypeError):
        logging.warning('hosts registry missing or malformed; overflow disabled, local RAM defaults 3000/2048 MB')
        return {'hosts': [], 'self': None}


def host_config(registry, name):
    for host in registry.get('hosts', []):
        if not isinstance(host, dict):
            continue
        aliases = host.get('aliases')
        names = [host.get('id'), *(aliases if isinstance(aliases, list) else [])]
        if not isinstance(name, str) or not any(
                isinstance(alias, str) and alias.lower() == name.lower() for alias in names):
            continue
        return host
    return {}


def placement_policy(registry, config):
    name = os.environ.get('PLANGRAPH_HOST_NAME') or registry.get('self')
    host = host_config(registry, name)
    name = host.get('id') or name
    hosts = config.get('hosts', {})
    local = hosts.get(name, {}) if isinstance(hosts, dict) else {}
    return name, (host.get('orchestration_only') is True or
                  isinstance(local, dict) and local.get('orchestration_only') is True)


def host_caps(registry, name, role='implement'):
    host = host_config(registry, name)
    if host:
        caps = host.get('caps')
        warmup = host.get('warmup')
        ramp = warmup.get('ramp_mb') if isinstance(warmup, dict) else None
        if (isinstance(caps, dict) and type(caps.get('mem_floor_mb')) is int
                and caps['mem_floor_mb'] >= 0 and isinstance(ramp, dict) and ramp
                and all(type(value) is int and value > 0 for value in ramp.values())):
            return ramp.get(role, max(ramp.values())), caps['mem_floor_mb']
    logging.warning('host %r: missing or malformed registry caps; not an overflow target', name)
    return None


def local_limits(registry, role='implement'):
    caps = host_caps(registry, os.environ.get('PLANGRAPH_HOST_NAME') or registry.get('self'), role)
    if caps is None:
        logging.warning('local admission using conservative RAM defaults: floor 3000 MB, role 2048 MB')
    return caps or (2048, 3000)


def memory_refusal(free_mb, reservation_mb, floor_mb):
    """Worker-core mem_fit: effective_free_mb - reservation_mb >= mem_floor_mb.

    These callers supply MemAvailable without unmaterialized slot reservations.
    The independent OOM guard remains in force.
    """
    if free_mb - reservation_mb >= floor_mb:
        return None
    return f'memory: free {free_mb} MB, floor {floor_mb} MB, role {reservation_mb} MB'


def memory_floor_record(memory, role_mb, floor, detail, role='implement'):
    return dict(run_id='run-' + uuid.uuid4().hex, status='refused', role=role,
                reason='host_memory_floor', overflow_reason=detail, exit_code=2,
                mem_available_mb=memory, floor_mb=floor, role_mb=role_mb, ended=time.time(),
                error=f'host_memory_floor: MemAvailable {memory} MB, floor {floor} MB, role {role_mb} MB; {detail}')


def decide(local_mem_mb, role_mb, floor_mb, ready_records, public, *,
            remote_first=False, local_refusal=None, host_order=()):
    """Select from config-enriched readiness records; age is computed on read."""
    local_reason = local_refusal or memory_refusal(local_mem_mb, role_mb, floor_mb)
    if not local_reason and not remote_first:
        return Decision(None, 'local room')
    if not public:
        if remote_first:
            return Decision(None, local_reason or 'local_fallback_no_remote')
        return Decision(None, local_reason + '; refused: repo is not public (absent or unknown allowlist)')
    candidates, refused = [], []
    records = ready_records.items() if remote_first else sorted(ready_records.items())
    for index, (name, row) in enumerate(records):
        reason = row.get('refusal')
        if not reason:
            if not row.get('ssh'):
                reason = 'no_route'
            elif row.get('ok') is not True:
                reason = 'failed readiness'
            elif row.get('fresh') is not True:
                reason = 'stale readiness'
            elif any(type(row.get(key)) is not int or row[key] < 0
                     for key in ('live_runs', 'mem_available_mb')):
                reason = 'malformed readiness'
            elif any(type(row.get(key)) is not int or row[key] < minimum
                     for key, minimum in (('role_mb', 1), ('floor_mb', 0))):
                reason = 'missing or malformed registry caps'
            else:
                reason = memory_refusal(row['mem_available_mb'], row['role_mb'], row['floor_mb'])
        if reason:
            refused.append(f'{name}: {reason}')
        else:
            if remote_first:
                index = host_order.index(name) if name in host_order else len(host_order)
            candidates.append((-(row['mem_available_mb'] - row['floor_mb']), index, name))
    if candidates:
        return Decision(min(candidates)[2], 'remote_most_free_ram' if remote_first else local_reason)
    if remote_first:
        return Decision(None, local_reason or 'local_fallback_no_remote')
    return Decision(None, local_reason + '; refused: ' + ('; '.join(refused) or 'no overflow hosts configured'))


def ready_records(config, role='implement'):
    records = {}
    registry = registry_caps()
    for name, host in config.get('hosts', {}).items():
        row = {}
        try:
            row = dict(host)
            if Path(name).name != name or name in ('.', '..'):
                raise ValueError('invalid host name')
            path = Path.home() / '.local/state/plangraph/host-ready' / (name + '.json')
            data = json.loads(path.read_text())
            stamp = datetime.fromisoformat(data['checked_at'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if data['host'] != name:
                raise ValueError('host mismatch')
            row.update({key: data.get(key) for key in ('ok', 'live_runs', 'mem_available_mb')})
            row['fresh'] = 0 <= age <= 600
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            row['refusal'] = 'missing or malformed readiness'
        caps = host_caps(registry, name, role)
        if caps is None:
            row.setdefault('refusal', 'missing or malformed registry caps')
        else:
            row.update(role_mb=caps[0], floor_mb=caps[1])
        records[name] = row
    return records


def local_resources(state=None):
    runs = cli.SpawnService(state=state).ps()['runs']
    return sum(row.get('status') == 'running' for row in runs), available_memory_mb()


def _windows_available_memory_mb():
    """MemAvailable's Windows twin: GlobalMemoryStatusEx ullAvailPhys."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                    *((name, ctypes.c_ulonglong) for name in (
                        'ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                        'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual'))]
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError('GlobalMemoryStatusEx failed')
    return status.ullAvailPhys // (1024 * 1024)


def available_memory_mb():
    # Windows has no /proc: every local spawn died with ENOENT before launch (2026-11-09).
    if os.name == 'nt':
        return _windows_available_memory_mb()
    try:
        memory = next(int(line.split()[1]) // 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.startswith('MemAvailable:'))
    except StopIteration as exc:
        raise ValueError('MemAvailable missing') from exc
    return memory


def swap_used_pct():
    try:
        values = {line.split(':')[0]: int(line.split()[1])
                  for line in Path('/proc/meminfo').read_text().splitlines()
                  if line.startswith(('SwapTotal:', 'SwapFree:'))}
        total, free = values['SwapTotal'], values['SwapFree']
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError('swap readings missing or malformed') from exc
    if not 0 <= free <= total:
        raise ValueError('invalid swap readings')
    return 100 * (total - free) / total if total else 0


def local_health_refusal(config, memory):
    if memory < config.get('local_min_available_mb', 4096):
        return 'local_min_available_mb'
    try:
        swap = swap_used_pct()
    except (OSError, ValueError):
        return 'local_swap_unavailable'
    if swap > config.get('local_max_swap_used_pct', 90):
        return 'local_max_swap_used_pct'
    return None


def remote_spawn(name, host, card, prompt, options):
    """Use private staging, quote every remote argument, never echo SSH stderr."""
    alias = host['ssh']
    def ssh(command):
        return subprocess.run(['ssh', '--', alias, command], capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
    staging = ssh('mktemp -d')
    if staging.returncode:
        raise RemoteRefusal('remote staging failed')
    directory = staging.stdout.strip()
    if not directory.startswith('/') or '\n' in directory:
        raise RemoteRefusal('remote staging path invalid')
    q = shlex.quote
    try:
        with tempfile.TemporaryDirectory() as local:
            rewritten = json.loads(json.dumps(card))
            diff = rewritten.get('diff_ref')
            if diff:
                diff['repo'] = host['repo']
            atomic_write_private(Path(local) / 'card.json', json.dumps(rewritten))
            atomic_write_private(Path(local) / 'prompt.txt', prompt)
            copied = subprocess.run(['scp', '--', str(Path(local) / 'card.json'),
                                     str(Path(local) / 'prompt.txt'), alias + ':' + q(directory + '/')],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace')
            if copied.returncode:
                raise RemoteRefusal('remote copy failed')
        commands = []
        if host.get('env_file'):
            commands.append('set -a && . ' + q(host['env_file']) + ' && set +a')
        commands.append('cd ' + q(host['repo']))
        if diff:
            for ref in ('base', 'head'):
                commands.append(shlex.join(['git', 'fetch', 'origin', '--', diff[ref]]))
        command = ['python3', '-m', 'plangraph.spawner.cli', 'spawn', '--card',
                   '@' + directory + '/card.json', '--prompt-file', directory + '/prompt.txt']
        spawn = shlex.join(command + options)
        nonce = secrets.token_hex(16)
        begin, end = (f'PLANGRAPH_RECEIPT_{nonce}_{suffix}' for suffix in ('BEGIN', 'END'))
        commands.append("printf '%s\\n' " + q(begin))
        commands.append('PLANGRAPH_HOST_NAME=' + q(name) + ' PYTHONPATH=' + q(host['spawner_src']) + ' ' + spawn)
        commands.append("printf '\\n%s\\n' " + q(end))
        result = ssh(' && '.join(commands))
        if result.returncode:
            raise RemoteRefusal('remote command failed')
        # Mask before parsing/exposing any remote output, including JSON keys.
        # Never retry an acknowledged spawn merely because its receipt is unreadable.
        output, _ = mask(result.stdout)
        lines = output.splitlines()
        if lines.count(begin) != 1 or lines.count(end) != 1 or lines.index(begin) >= lines.index(end):
            return {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'unknown',
                    'host': name, 'acknowledged_unknown': True,
                    'reason': 'receipt_unbracketed'}
        try:
            receipt = json.loads('\n'.join(lines[lines.index(begin) + 1:lines.index(end)]))
        except ValueError:
            receipt = None
        if (isinstance(receipt, dict) and isinstance(receipt.get('run_id'), str)
                and receipt['run_id'].strip()):
            return receipt
        return {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'unknown',
                'host': name, 'acknowledged_unknown': True}
    finally:
        ssh('rm -f -- ' + q(directory + '/card.json') + ' ' + q(directory + '/prompt.txt') +
            '; rmdir -- ' + q(directory))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('verb', choices=['spawn'])
    parser.add_argument('--repo', required=True)
    parser.add_argument('--card', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--role', default='implement')
    parser.add_argument('--state')
    args, options = parser.parse_known_args(argv)
    if args.state is not None: options.extend(['--state', args.state])
    if args.role != 'implement':
        options.extend(['--role', args.role])
    try:
        path = Path(os.environ.get('PLANGRAPH_OVERFLOW_HOSTS', '~/.config/plangraph/overflow-hosts.json')).expanduser()
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            config = {}
        if placement_policy(registry_caps(), config)[1]:
            return cli.main(['spawn', '--card', '@' + args.card,
                             Path(args.prompt).read_text(), *options])
        role_mb, floor_mb = local_limits(registry_caps(), args.role)
        _, memory = local_resources(args.state) if args.state is not None else local_resources()
        if decide(memory, role_mb, floor_mb, {}, False).reason == 'local room':
            return cli.main(['spawn', '--card', '@' + args.card, Path(args.prompt).read_text(), *options])
        public = str(Path(args.repo).resolve()) in [str(Path(p).expanduser().resolve())
                                                  for p in config.get('public_repos', [])]
        card = json.loads(Path(args.card).read_text())
        diff = card.get('diff_ref')
        if diff and Path(diff['repo']).resolve() != Path(args.repo).resolve():
            public = False
        prompt = Path(args.prompt).read_text()
        records = ready_records(config, args.role)
        attempts = 0
        while True:
            decision = decide(memory, role_mb, floor_mb, records, public)
            if decision.host is None or attempts == 2:
                detail = decision.reason if decision.host is None else 'refused: retry limit'
                record = memory_floor_record(memory, role_mb, floor_mb, detail, args.role)
                if not memory_refusal(memory, role_mb, floor_mb): record.update(reason=detail, error=detail)
                cli.SpawnService(state=args.state)._save(record)
                print(json.dumps(record))
                print(record['error'], file=sys.stderr)
                return 2
            name = decision.host
            attempts += 1
            try:
                result = remote_spawn(name, config['hosts'][name], card, prompt, options)
                refusal = 'remote spawn failed'
            except RemoteRefusal as exc:
                result, refusal = None, str(exc)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result, refusal = None, failure_reason(exc)
            if result is not None:
                result.update(host=name, overflow_reason=decision.reason)
                print(mask(json.dumps(result))[0])
                return 0
            records[name]['refusal'] = refusal
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('overflow input or resource check failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
