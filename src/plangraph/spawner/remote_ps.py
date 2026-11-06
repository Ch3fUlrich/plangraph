"""Optional, bounded SSH fan-out for ps; transport diagnostics stay private."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess

TIMEOUT = 3
FIELDS = ('run_id', 'host', 'status', 'role', 'started', 'ended', 'pid', 'exit_code')


def query_host(host, timeout):
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
         '--', host['target'], 'python3 -m plangraph.spawner.remote_ps'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace', timeout=timeout, check=True)
    return result.stdout


def configured_hosts():
    path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    try:
        data = json.loads((path / 'plangraph' / 'ps-hosts.json').read_text())
    except FileNotFoundError:
        return []
    hosts = data['hosts']
    if not isinstance(hosts, list):
        raise ValueError('invalid ps host configuration')
    for host in hosts:
        if (not isinstance(host, dict)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', host.get('name', ''))
                or not isinstance(host.get('target'), str)
                or not re.fullmatch(r'[A-Za-z0-9_@.:-]+', host['target'])
                or host['target'].startswith('-')):
            raise ValueError('invalid ps host configuration')
    return hosts


def _fetch(host, transport):
    try:
        data = json.loads(transport(host, TIMEOUT))
        rows = data['runs']
        if not isinstance(rows, list):
            raise ValueError()
        clean = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('run_id'), str)
                    or not isinstance(row.get('host'), str)
                    or not isinstance(row.get('status'), str)):
                raise ValueError()
            if row['status'] in ('running', 'queued'):
                clean.append({k: row[k] for k in FIELDS if k in row})
        return clean
    except Exception:
        return [dict(host=host['name'], status='unknown', reason='host query failed')]


def list_runs(local, *, transport=query_host):
    hosts = configured_hosts()
    if not hosts:
        return local
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        results = pool.map(lambda host: _fetch(host, transport), hosts)
        return list(local) + [row for rows in results for row in rows]


if __name__ == '__main__':
    # Local-only protocol endpoint: never invoke ps (which would fan out again).
    from .service import SpawnService
    from .stall_watchdog import read_runs
    print(json.dumps({'runs': [{k: r[k] for k in FIELDS if k in r}
                              for r in read_runs(SpawnService().state)]}))
