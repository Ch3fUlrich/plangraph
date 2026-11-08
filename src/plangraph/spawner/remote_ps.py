"""Optional, bounded SSH fan-out for ps; transport diagnostics stay private."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import selectors
import shlex
import time
from .runner import is_terminal

MAX_OUTPUT = 1024 * 1024
MAX_WORKERS = 16

TIMEOUT = 3
FIELDS = ('run_id', 'host', 'status', 'role', 'started', 'ended', 'pid', 'exit_code')
FIELD_TYPES = dict(run_id=(str,), host=(str,), status=(str,), role=(str,),
                   started=(int, float), ended=(int, float, type(None)),
                   pid=(int, type(None)), exit_code=(int, type(None)))


class HostQueryError(ValueError):
    """Only fixed public reasons cross the transport boundary."""


SRC_MISSING = 44


def source_check(host):
    if 'win_src' in host:
        src = host['win_src']
        if not isinstance(src, str) or not re.fullmatch(r'[A-Za-z]:[/\\][A-Za-z0-9_ ./\\-]*', src):
            raise ValueError('invalid configuration')
        return f'if not exist "{src}\\" exit /b {SRC_MISSING}'
    return f'test -d {source_path(host["src"])} || exit {SRC_MISSING}'


def source_path(src):
    if src.startswith('~/'):
        return '"$HOME"/' + shlex.quote(src[2:])
    return shlex.quote(src)


def host_command(host, *, query=False):
    """Build the same source probe and shell wrapper for both callers."""
    module = 'python3 -m plangraph.spawner.remote_ps'
    if 'win_src' in host:
        command = source_check(host)
        if query:
            command += (f' & set "PYTHONPATH={host["win_src"]}"&& '
                        'python -m plangraph.spawner.remote_ps')
        return command
    command = source_check(host) if 'src' in host else ''
    if query:
        command += f'; PYTHONPATH={source_path(host["src"])} {module}' if command else module
    if 'wsl_distro' in host:
        distro = host['wsl_distro']
        if not isinstance(distro, str) or not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]*', distro):
            raise ValueError('invalid configuration')
        command = f'wsl -d {distro} -- sh -c {shlex.quote(command)}'
    return command


def exit_reason(code):
    return {0: None, SRC_MISSING: 'host src missing', 255: 'host unreachable'}.get(
        code, 'host query failed')


def query_host(host, timeout):
    command = host_command(host, query=True)
    with subprocess.Popen(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
         '--', host['target'], command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
        deadline = time.monotonic() + timeout
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    events = selector.select(max(0, remaining))
                    if remaining <= 0 or not events:
                        raise TimeoutError('host query failed')
                    for key, _ in events:
                        buffer = output
                        chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(buffer)))
                        buffer.extend(chunk)
                        if len(buffer) > MAX_OUTPUT:
                            raise ValueError('host query failed')
                        if not chunk:
                            selector.unregister(key.fileobj)
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
            reason = exit_reason(code)
            if reason:
                raise HostQueryError(reason)
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
                 or host['target'].startswith('-')
                 or ('win_src' in host and (
                     not isinstance(host['win_src'], str)
                      or not re.fullmatch(r'[A-Za-z]:[/\\][A-Za-z0-9_ ./\\-]*',
                                         host['win_src'])
                     or 'src' in host or 'wsl_distro' in host))
                or ('src' in host and (
                    not isinstance(host['src'], str)
                     or not re.fullmatch(r'(?:/|~/)[A-Za-z0-9_ ./-]*', host['src'])))
                or ('wsl_distro' in host and (
                    not isinstance(host['wsl_distro'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]*',
                                        host['wsl_distro'])
                    or not isinstance(host.get('src'), str)
                     or not re.fullmatch(r'/[A-Za-z0-9_ ./-]*', host['src'])))):
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
    except HostQueryError as exc:
        reason = str(exc) if str(exc) in ('host src missing', 'host unreachable') else 'host query failed'
        return [dict(host=host['name'], status='unknown', reason=reason)]
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
