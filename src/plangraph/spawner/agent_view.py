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

from plangraph.agent_view_projection import FIELDS, load_view, project, read, unavailable
FINISHED = {'done', 'stopped', 'completed', 'failed', 'cancelled', 'exited'}


def local(home=None, host=None, now=None):
    home = Path(home or Path.home())
    host = host or socket.gethostname()
    now = time.time() if now is None else now
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE') or
                 Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'plangraph')
    agents = []

    def ingest(path, runtime):
        try:
            source = read(path)
            row = project(source, host, runtime, now)
            if source.get('stopped_ts') is not None or source.get('ended') is not None:
                return
            if row['state'] not in FINISHED:
                agents.append(row)
        except (OSError, ValueError, TypeError):
            agents.append(project({}, host))

    try:
        for pattern, runtime in [('runs/*/record.json', 'spawner-run'),
                                 ('sessions/*.json', 'claude')]:
            for path in state.glob(pattern):
                if path.name != '_counters.json' and not path.name.endswith('.ready.json'):
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
        agents.append(project({}, host))
    return dict(host=host, readable=True, reason=None, agents=agents)


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
                      '--', config['ssh'], command], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        if result.returncode:
            return unavailable(host, 'ssh_failed')
        data = json.loads(result.stdout)['hosts'][0]
        if data.get('readable') is not True:
            return unavailable(host, 'source_unreadable')
        return dict(host=host, readable=True, reason=None,
                    agents=[project(row, host) for row in data['agents']])
    except subprocess.TimeoutExpired:
        return unavailable(host, 'timeout')
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
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
