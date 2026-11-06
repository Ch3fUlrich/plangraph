"""Read-only fleet agent inventory; no session APIs or runtime commands."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time

from plangraph.routing_support.private_io import atomic_write_private

FIELDS = ('host', 'name', 'runtime', 'model', 'state', 'item', 'age_s')
FINISHED = {'done', 'stopped', 'completed', 'failed', 'cancelled', 'exited'}


def read(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('expected object')
    return value


def project(source, host, runtime=None, now=None):
    """Whitelist scalars, including when reading a remote result."""
    row = {k: source.get(k) if isinstance(source.get(k), (str, int, float))
           and not isinstance(source.get(k), bool) else None for k in FIELDS}
    row['host'] = host
    row['runtime'] = runtime or (row['runtime'] if row['runtime'] in
                               ('claude', 'opencode', 'spawner-run') else None)
    if now is not None:
        for field, alias in [('name', 'run_id'), ('item', 'item_key')]:
            if row[field] is None and isinstance(source.get(alias), str):
                row[field] = source[alias]
        row['state'] = source.get('state', source.get('status'))
        if not isinstance(row['state'], str):
            row['state'] = None
        stamp = source.get('started', source.get('launched_ts', source.get('createdAt')))
        try:
            if isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
            row['age_s'] = max(0, now - float(stamp))
        except (ValueError, TypeError, OverflowError):
            row['age_s'] = None
    return row


def unavailable(host, reason):
    return dict(host=host, readable=False, reason=reason, agents=[])


def local(home=None, host=None, now=None):
    home = Path(home or Path.home())
    host = host or socket.gethostname()
    now = time.time() if now is None else now
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE') or
                 Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'plangraph')
    agents = []
    broken = False

    def ingest(path, runtime):
        nonlocal broken
        try:
            source = read(path)
            row = project(source, host, runtime, now)
            if source.get('stopped_ts') is not None or source.get('ended') is not None:
                return
            if row['state'] not in FINISHED:
                agents.append(row)
        except (OSError, ValueError, TypeError):
            broken = True

    try:
        for pattern, runtime in [('runs/*/record.json', 'spawner-run'),
                                 ('sessions/*.json', 'claude')]:
            for path in state.glob(pattern):
                ingest(path, runtime)
        for path in (home / '.claude/jobs').glob('*/state.json'):
            ingest(path, 'claude')
        config = home / '.config/autoos/oc-l1.json'
        if config.exists():
            lane_config = read(config)
            lanes = lane_config.get('lanes', lane_config)
            for lane in lanes.values() if isinstance(lanes, dict) else lanes:
                if not isinstance(lane, dict):
                    raise ValueError('bad lane')
                if lane.get('state_file'):
                    path = Path(lane['state_file']).expanduser()
                    if path.exists():
                        ingest(path, 'opencode')
    except (OSError, ValueError, TypeError):
        broken = True
    return dict(host=host, readable=not broken,
                reason='source_unreadable' if broken else None, agents=agents)


def remote(host, config, run=subprocess.run, timeout=20):
    if not isinstance(config, dict) or not config.get('ssh'):
        return unavailable(host, 'no_route')
    try:
        src = shlex.quote(config['spawner_src'])
        command = 'cd ' + src + ' && '
        if config.get('env_file'):
            command += 'set -a && . ' + shlex.quote(config['env_file']) + ' && set +a && '
        command += 'PYTHONPATH=src python3 -m plangraph.spawner.agent_view --json --local'
        result = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                      '--', config['ssh'], command], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            return unavailable(host, 'ssh_failed')
        data = json.loads(result.stdout)['hosts'][0]
        if data.get('readable') is not True:
            return unavailable(host, 'source_unreadable')
        return dict(host=host, readable=True, reason=None,
                    agents=[project(row, host) for row in data['agents']])
    except subprocess.TimeoutExpired:
        return unavailable(host, 'timeout')
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return unavailable(host, 'invalid_response')


def collect(home=None, run=subprocess.run, timeout=20, local_only=False):
    home = Path(home or Path.home())
    hosts = [local(home)]
    config = Path(os.environ.get('PLANGRAPH_OVERFLOW_HOSTS') or
                  home / '.config/plangraph/overflow-hosts.json')
    if not local_only and config.exists():
        try:
            entries = read(config)['hosts']
            with ThreadPoolExecutor(max_workers=max(1, len(entries))) as pool:
                futures = [pool.submit(remote, name, cfg, run, timeout)
                           for name, cfg in entries.items()]
                hosts.extend(f.result() for f in futures)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            hosts.append(unavailable('unknown', 'config_unreadable'))
    return dict(generated_at=datetime.now(timezone.utc).isoformat(), hosts=hosts)


def write(data, home=None):
    path = Path(home or Path.home()) / '.local/state/plangraph/agents-view.json'
    atomic_write_private(path, json.dumps(data) + '\n')


def load_view(home=None):
    """Re-project the private file before embedding it in a public page."""
    try:
        data = read(Path(home or Path.home()) / '.local/state/plangraph/agents-view.json')
        hosts = []
        for entry in data['hosts']:
            host = entry['host'] if isinstance(entry['host'], str) else 'unknown'
            if entry.get('readable') is not True:
                hosts.append(unavailable(host, 'unreadable'))
            else:
                hosts.append(dict(host=host, readable=True, reason=None,
                                  agents=[project(r, host) for r in entry['agents']]))
        return dict(generated_at=data.get('generated_at') if isinstance(
            data.get('generated_at'), str) else None, hosts=hosts)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--local', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    data = collect(local_only=args.local)
    if args.write:
        write(data)
    print(json.dumps(data, indent=None if args.json else 2))


if __name__ == '__main__':
    main()
