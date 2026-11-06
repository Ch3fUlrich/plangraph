"""Default local admission with opt-in public-repository SSH overflow."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from typing import NamedTuple

from . import cli
from .private_io import atomic_write_private


class Decision(NamedTuple):
    host: str | None
    reason: str


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
            elif row['live_runs'] >= row.get('run_cap', 4):
                reason = 'run cap'
            elif row['mem_available_mb'] < row.get('floor_mb', 3000):
                reason = 'memory floor'
        if reason:
            refused.append(f'{name}: {reason}')
        else:
            candidates.append((-(row.get('run_cap', 4) - row['live_runs']), -row['mem_available_mb'], name))
    if candidates:
        return Decision(min(candidates)[2], 'local run cap or memory floor')
    return Decision(None, 'queue: ' + ('; '.join(refused) or 'no overflow hosts configured'))


def ready_records(config):
    records = {}
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
        return None
    directory = staging.stdout.strip()
    if not directory.startswith('/') or '\n' in directory:
        return None
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
                return None
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
        commands.append('PLANGRAPH_HOST_NAME=' + q(name) + ' PYTHONPATH=' + q(host['spawner_src']) + ' ' + spawn)
        result = ssh(' && '.join(commands))
        if result.returncode:
            return None
        # Decode objects amid shell noise, including pretty-printed JSON. Never
        # retry an acknowledged spawn merely because its receipt is unreadable.
        decoder = json.JSONDecoder()
        receipt, furthest = None, -1
        for index in [i for i, char in enumerate(result.stdout) if char == '{']:
            try:
                payload, end = decoder.raw_decode(result.stdout[index:])
                if isinstance(payload, dict) and index + end > furthest:
                    receipt, furthest = payload, index + end
            except ValueError:
                pass
        # Shell initialization may echo JSON; only expose a spawn receipt.
        if receipt is not None and ('status' in receipt or receipt == {}):
            return receipt
        return {'warning': 'remote spawn succeeded; receipt unavailable'}
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
        limits = []
        for key, default in (('local_run_cap', 4), ('local_floor_mb', 3000)):
            try:
                limits.append(int(config.get(key, default)))
            except (TypeError, ValueError, OverflowError):
                limits.append(default)
        run_cap, floor_mb = limits
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
            except (OSError, ValueError, KeyError):
                result = None
            if result is not None:
                result.update(host=name, overflow_reason=decision.reason)
                print(json.dumps(result))
                return 0
            records[name]['refusal'] = 'remote spawn failed'
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('overflow input or resource check failed', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
