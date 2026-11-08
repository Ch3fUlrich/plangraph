"""Default local admission with opt-in public-repository SSH overflow."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
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


def registry_caps():
    """Read the same registry location and JSON format as fleet-sync."""
    try:
        registry = json.loads((config_dir() / 'hosts.json').read_text())
        if not isinstance(registry['hosts'], list):
            raise ValueError('hosts must be a list')
        return registry
    except (OSError, ValueError, KeyError, TypeError):
        logging.warning('hosts registry missing or malformed; overflow disabled, local defaults 4/3000')
        return {'hosts': [], 'self': None}


def host_caps(registry, name):
    for host in registry['hosts']:
        if not isinstance(host, dict):
            continue
        aliases = host.get('aliases')
        names = [host.get('id'), *(aliases if isinstance(aliases, list) else [])]
        if not isinstance(name, str) or not any(
                isinstance(alias, str) and alias.lower() == name.lower() for alias in names):
            continue
        caps = host.get('caps')
        if isinstance(caps, dict) and all(type(caps.get(key)) is int and caps[key] >= minimum
                                         for key, minimum in (('max_runs', 1), ('mem_floor_mb', 0))):
            return caps['max_runs'], caps['mem_floor_mb']
        break
    logging.warning('host %r: missing or malformed registry caps; not an overflow target', name)
    return None


def local_limits(registry):
    caps = host_caps(registry, os.environ.get('PLANGRAPH_HOST_NAME') or registry.get('self'))
    if caps is None:
        logging.warning('local admission using conservative defaults: 4 runs / 3000 MB')
    return caps or (4, 3000)


def decide(local_live, local_mem_mb, run_cap, floor_mb, ready_records, public):
    """Select from config-enriched readiness records; age is computed on read."""
    if local_live < run_cap and local_mem_mb >= floor_mb:
        return Decision(None, 'local room')
    if not public:
        return Decision(None, 'queue: repo is not public (absent or unknown allowlist)')
    candidates, refused = [], []
    for name, row in sorted(ready_records.items()):
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
                     for key, minimum in (('run_cap', 1), ('floor_mb', 0))):
                reason = 'missing or malformed registry caps'
            elif row['live_runs'] >= row['run_cap']:
                reason = 'run cap'
            elif row['mem_available_mb'] < row['floor_mb']:
                reason = 'memory floor'
        if reason:
            refused.append(f'{name}: {reason}')
        else:
            candidates.append((-(row['run_cap'] - row['live_runs']), -row['mem_available_mb'], name))
    if candidates:
        return Decision(min(candidates)[2], 'local run cap or memory floor')
    return Decision(None, 'queue: ' + ('; '.join(refused) or 'no overflow hosts configured'))


def ready_records(config):
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
        caps = host_caps(registry, name)
        if caps is None:
            row.setdefault('refusal', 'missing or malformed registry caps')
        else:
            row.update(run_cap=caps[0], floor_mb=caps[1])
        records[name] = row
    return records


def local_resources():
    runs = cli.SpawnService().ps()['runs']
    try:
        memory = next(int(line.split()[1]) // 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.startswith('MemAvailable:'))
    except StopIteration as exc:
        raise ValueError('MemAvailable missing') from exc
    return sum(row.get('status') == 'running' for row in runs), memory


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
                   '@' + directory + '/card.json']
        # A sentinel prevents command substitution from stripping trailing newlines.
        commands.append('prompt=$(cat ' + q(directory + '/prompt.txt') + ' && printf .)')
        spawn = shlex.join(command) + ' "${prompt%.}" ' + shlex.join(options)
        token = uuid.uuid4().hex
        begin, end = 'PLANGRAPH_RECEIPT_BEGIN_' + token, 'PLANGRAPH_RECEIPT_END_' + token
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
            return {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'unknown', 'host': name,
                    'acknowledged_unknown': True, 'reason': 'receipt_unbracketed'}
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
    args, options = parser.parse_known_args(argv)
    try:
        path = Path(os.environ.get('PLANGRAPH_OVERFLOW_HOSTS', '~/.config/plangraph/overflow-hosts.json')).expanduser()
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            config = {}
        run_cap, floor_mb = local_limits(registry_caps())
        live, memory = local_resources()
        if decide(live, memory, run_cap, floor_mb, {}, False).reason == 'local room':
            return cli.main(['spawn', '--card', '@' + args.card, Path(args.prompt).read_text(), *options])
        public = str(Path(args.repo).resolve()) in [str(Path(p).expanduser().resolve())
                                                  for p in config.get('public_repos', [])]
        card = json.loads(Path(args.card).read_text())
        diff = card.get('diff_ref')
        if diff and Path(diff['repo']).resolve() != Path(args.repo).resolve():
            public = False
        prompt = Path(args.prompt).read_text()
        records = ready_records(config)
        attempts = 0
        while True:
            decision = decide(live, memory, run_cap, floor_mb, records, public)
            if decision.host is None:
                print(json.dumps({'overflow_reason': decision.reason}))
                return 5
            if attempts == 2:
                print(json.dumps({'overflow_reason': 'queue: ' + '; '.join(
                    name + ': ' + row.get('refusal', 'retry limit') for name, row in sorted(records.items()))}))
                return 5
            name = decision.host
            attempts += 1
            try:
                result = remote_spawn(name, config['hosts'][name], card, prompt, options)
                refusal = 'remote spawn failed'
            except RemoteRefusal as exc:
                result, refusal = None, str(exc)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result, refusal = None, 'remote spawn ' + type(exc).__name__
            if result is not None:
                result.update(host=name, overflow_reason=decision.reason)
                print(mask(json.dumps(result))[0])
                return 0
            records[name]['refusal'] = refusal
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('overflow input or resource check failed', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
