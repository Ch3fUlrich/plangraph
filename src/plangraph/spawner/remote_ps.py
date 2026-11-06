"""Optional, bounded SSH fan-out for ps; transport diagnostics stay private."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import selectors
import time
from .runner import is_terminal

MAX_OUTPUT = 1024 * 1024
MAX_WORKERS = 16

TIMEOUT = 3
FIELDS = ('run_id', 'host', 'status', 'role', 'started', 'ended', 'pid', 'exit_code')
FIELD_TYPES = dict(run_id=(str,), host=(str,), status=(str,), role=(str,),
                   started=(int, float), ended=(int, float, type(None)),
                   pid=(int, type(None)), exit_code=(int, type(None)))


def query_host(host, timeout):
    with subprocess.Popen(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
         '--', host['target'], 'python3 -m plangraph.spawner.remote_ps'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
        deadline = time.monotonic() + timeout
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError('host query failed')
                    chunk = os.read(process.stdout.fileno(), min(65536, MAX_OUTPUT + 1 - len(output)))
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise ValueError('host query failed')
                    if not chunk:
                        break
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise ValueError('host query failed')
            return output.decode('utf-8', errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def configured_hosts():
    path = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    try:
        data = json.loads((path / 'plangraph' / 'ps-hosts.json').read_text())
    except FileNotFoundError:
        return []
    hosts = data['hosts']
    if not isinstance(hosts, list):
        raise ValueError('invalid ps host configuration')
    validated = []
    for host in hosts:
        if (not isinstance(host, dict)
                or not isinstance(host.get('name'), str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', host.get('name', ''))
                or not isinstance(host.get('target'), str)
                or not re.fullmatch(r'[A-Za-z0-9_@.:-]+', host['target'])
                or host['target'].startswith('-')):
            validated.append(dict(name='invalid-config', invalid=True))
        else:
            validated.append(host)
    return validated


def _fetch(host, transport):
    try:
        if host.get('invalid'):
            raise ValueError()
        data = json.loads(transport(host, TIMEOUT))
        rows = data['runs']
        if not isinstance(rows, list):
            raise ValueError()
        clean = []
        for row in rows:
            if isinstance(row, dict) and is_terminal(row.get('status')):
                continue
            if (not isinstance(row, dict)
                    or not all(k in row for k in ('run_id', 'host', 'status'))
                    or any(type(row[k]) not in FIELD_TYPES[k]
                           for k in FIELDS if k in row)
                    or not 1 <= len(row['host']) <= 253
                    or not row['run_id']
                    or any(any(ord(c) < 32 or 127 <= ord(c) <= 159 or
                               0xD800 <= ord(c) <= 0xDFFF for c in row[k])
                           for k in FIELDS if k in row and isinstance(row[k], str))):
                raise ValueError()
            clean.append({k: row[k] for k in FIELDS if k in row})
        return clean
    except Exception:
        return [dict(host=host['name'], status='unknown', reason='host query failed')]


def list_runs(local, *, transport=query_host):
    try:
        hosts = configured_hosts()
    except (OSError, ValueError, KeyError, TypeError):
        # Keep local rows, but distinguish broken configuration from no hosts.
        return list(local) + [dict(host='invalid-config', status='unknown',
                                   reason='host query failed')]
    if not hosts:
        return local
    try:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(hosts))) as pool:
            results = pool.map(lambda host: _fetch(host, transport), hosts)
            return list(local) + [row for rows in results for row in rows]
    except RuntimeError:
        return list(local) + [dict(host=h['name'], status='unknown',
                                  reason='host query failed') for h in hosts]


def collect_local(state):
    # Local-only protocol endpoint: never invoke ps (which would fan out again).
    from .stall_watchdog import read_runs
    return {'runs': [{k: r[k] for k in FIELDS if k in r} for r in read_runs(state)
                     if not is_terminal(r.get('status'))]}


if __name__ == '__main__':
    from .service import SpawnService
    print(json.dumps(collect_local(SpawnService().state)))
