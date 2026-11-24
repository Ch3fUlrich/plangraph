"""Optional, bounded SSH fan-out for ps; transport diagnostics stay private."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import selectors
import secrets
import shlex
import time
from .item_key import MAX_BATCH_KEYS, valid_item
from .runner import is_terminal

MAX_OUTPUT = 1024 * 1024
MAX_WORKERS = 16

TIMEOUT = 3
FIELDS = ('run_id', 'host', 'status', 'role', 'started', 'ended', 'pid', 'exit_code', 'item_key', 'item_keys')
FIELD_TYPES = dict(run_id=(str,), host=(str,), status=(str,), role=(str,),
                   started=(int, float), ended=(int, float, type(None)),
                   pid=(int, type(None)), exit_code=(int, type(None)), item_key=(str, type(None)), item_keys=(list,))


class HostQueryError(ValueError):
    """Transport failure with a public SSH exit code; stderr stays private."""

    def __init__(self, reason, *, code=None, stderr=''):
        super().__init__(reason)
        self.code, self.stderr = code, stderr


def safe_diagnostic(text):
    """Only structured transport metadata is public; stderr stays private."""
    if isinstance(text, str) and re.fullmatch(
            r'(?:HostQueryError \(SSH exit (?:[0-9]{1,3})\)|'
            r'HostQueryError|TimeoutError|TimeoutExpired|FileNotFoundError|'
            r'PermissionError|OSError|ValueError)', text):
        return text
    return 'host query failed'


def safe_reason(exc):
    """Keep the fixed reason contract, regardless of exception text."""
    if isinstance(exc, HostQueryError):
        return exit_reason(exc.code) or 'host query failed'
    return 'host query failed'


SRC_MISSING = 44


def source_check(host):
    if 'win_src' in host:
        src = host['win_src']
        if not isinstance(src, str) or not re.fullmatch(r'[A-Za-z]:[/\\][A-Za-z0-9_ ./\\-]*', src):
            raise ValueError('invalid configuration')
        src = src.replace('/', '\\')
        return f'if not exist "{src}\\" exit /b {SRC_MISSING}'
    return f'test -d {source_path(host["src"])} || exit {SRC_MISSING}'


def source_path(src):
    if src.startswith('~/'):
        return '"$HOME"/' + shlex.quote(src[2:])
    return shlex.quote(src)


WSL_RUNNER = 'pg-spawnd steps'
SHELL_SYNTAX = re.compile(r'[;&|<>$`(){}\\\n]')


def wsl_distro(host):
    distro = host.get('wsl_distro')
    if distro is not None and not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]*', distro):
        raise ValueError('invalid WSL distribution')
    return distro


def plan_body(script, steps=None, **plan):
    """The JSON step plan for `pg-spawnd steps`: `steps`, or the one argv of a simple `script`."""
    if steps is None:
        if SHELL_SYNTAX.search(script):
            raise ValueError('compound script needs a step plan')
        steps = [{'argv': shlex.split(script)}]
    return json.dumps({**plan, 'steps': steps}).encode()


def invocation(host, script, steps=None, **plan):
    """The remote command and its stdin: a script for a POSIX login shell, or a step plan.

    A WSL guest is reached through `wsl.exe --exec`, which runs no shell, so it gets the plan
    as JSON on stdin for `pg-spawnd steps`.
    """
    distro = wsl_distro(host)
    if distro is None:
        return script, None
    return f'wsl.exe --distribution {distro} --exec {WSL_RUNNER}', plan_body(script, steps, **plan)


def host_invocation(host, *, query=False):
    """Same source probe for both callers; Windows hosts keep their cmd.exe text."""
    module = ['python3', '-m', 'plangraph.spawner.remote_ps']
    if 'win_src' in host:
        return host_command(host, query=query), None
    steps = []
    if 'src' in host:
        steps.append({'argv': ['test', '-d', host['src']], 'fail_code': SRC_MISSING})
    if query:
        steps.append({'argv': module, 'env': {'PYTHONPATH': host['src']}} if 'src' in host
                     else {'argv': module})
    return invocation(host, host_command(host, query=query), steps)


def host_command(host, *, query=False):
    """Build the same source probe for both callers (the POSIX or cmd.exe script)."""
    module = 'python3 -m plangraph.spawner.remote_ps'
    if 'win_src' in host:
        command = source_check(host)
        if query:
            command = f'({command})'
            src = host['win_src'].replace('/', '\\')
            command += (f' & set "PYTHONPATH={src}"&& '
                        'python -m plangraph.spawner.remote_ps')
        return command
    command = source_check(host) if 'src' in host else ''
    if query:
        command += f'; PYTHONPATH={source_path(host["src"])} {module}' if command else module
    return command


def exit_reason(code):
    return {0: None, SRC_MISSING: 'host src missing', 255: 'host unreachable'}.get(
        code, 'host query failed')


def query_host(host, timeout):
    cursor = host.get('lifecycle_cursor')
    day = host.get('coverage_day')
    if day is not None:
        argv = ['pg-spawnd', 'attempt-coverage-input', day]
    elif cursor is not None:
        argv = ['pg-spawnd', 'runs', '--changed-since', str(cursor)]
    else:
        argv = None
    if argv is None:
        command, plan = host_invocation(host, query=True)
    else:
        command, plan = invocation(host, shlex.join(argv), [{'argv': argv}])
    with subprocess.Popen(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
         '--', host['target'], command],
        stdin=subprocess.DEVNULL if plan is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
        deadline = time.monotonic() + timeout
        output = bytearray()
        pending = memoryview(plan or b'')
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if plan is not None:
                    # Nonblocking writes in the same loop: a reader that never drains stdin,
                    # even through a descendant that outlives the child, cannot pass the deadline.
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                reading = True
                while reading:
                    remaining = deadline - time.monotonic()
                    events = selector.select(max(0, remaining))
                    if remaining <= 0 or not events:
                        raise TimeoutError('host query failed')
                    for key, _ in events:
                        if key.fileobj is process.stdin:
                            try:
                                pending = pending[os.write(key.fd, pending[:65536]):]
                            except BlockingIOError:
                                continue
                            except OSError:
                                pending = pending[len(pending):]  # The exit status names the failure.
                            if not pending:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                            continue
                        buffer = output
                        chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(buffer)))
                        buffer.extend(chunk)
                        if len(buffer) > MAX_OUTPUT:
                            raise ValueError('host query failed')
                        if not chunk:
                            selector.unregister(key.fileobj)
                            reading = False
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
            reason = exit_reason(code)
            if reason:
                raise HostQueryError(reason, code=code)
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
            return [dict(host=host['name'], status='unknown', reason='host query failed')]
        try:
            payload = transport(host, TIMEOUT)
        except Exception as exc:
            error = type(exc).__name__
            if isinstance(exc, HostQueryError) and type(exc.code) is int and 0 <= exc.code <= 255:
                error += f' (SSH exit {exc.code})'
            return [dict(host=host['name'], status='unknown', reason=safe_reason(exc),
                         error=safe_diagnostic(error))]
        data = json.loads(payload)
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
                    or ('item_keys' in row and (
                        not 1 <= len(row['item_keys']) <= MAX_BATCH_KEYS
                        or not all(valid_item(key) for key in row['item_keys'])))
                    or any(any(ord(c) < 32 or 127 <= ord(c) <= 159 or
                               0xD800 <= ord(c) <= 0xDFFF for c in row[k])
                           for k in FIELDS if k in row and isinstance(row[k], str))):
                raise ValueError()
            clean.append(dict({k: row[k] for k in FIELDS if k in row}, host=host['name']))
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


_LIFECYCLE_PUBLISHER = 'PLANGRAPH_LIFECYCLE_PUBLISHER'


def lifecycle_command(state, *args, **kwargs):
    from plangraph.fleet_snapshot import _projection_binary
    binary = os.environ.get(_LIFECYCLE_PUBLISHER) or _projection_binary()
    return subprocess.run([binary, *args], check=True, timeout=10,
                          env=dict(os.environ, PLANGRAPH_SPAWN_STATE=str(state)), **kwargs)

def attempt_coverage_inputs(state, day, *, transport=query_host):
    """Collect the same closed UTC day from the local and configured remote hosts."""
    def decode(payload):
        result = json.loads(payload)
        if result['day'] != day or not all(isinstance(result[k], list) for k in ('runs', 'fleet')):
            raise ValueError('invalid attempt coverage input')
        return result

    evidence = decode(lifecycle_command(state, 'attempt-coverage-input', day,
                                        stdout=subprocess.PIPE).stdout)

    def fetch(host):
        if host.get('invalid'):
            raise ValueError('invalid attempt coverage host configuration')
        return decode(transport(dict(host, coverage_day=day), TIMEOUT))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for remote in pool.map(fetch, configured_hosts()):
            for source in ('runs', 'fleet'):
                evidence[source].extend(remote[source])
    return evidence


def publish_run_record(state, path, body):
    if os.environ.get(_LIFECYCLE_PUBLISHER):
        # A configured publisher owns publication. A failure leaves its
        # journal obligation recoverable instead of replacing the projection.
        lifecycle_command(state, 'record-publish', input=body, text=True,
                          encoding='utf-8', errors='replace')
    else:
        from .private_io import atomic_write_private
        atomic_write_private(path, body)


def changed_runs(state, bookmark, *, transport=query_host):
    """Read independent host cursors; a failed host keeps its last bookmark."""
    if not isinstance(bookmark, dict) or set(bookmark) - {'local', 'hosts'}:
        raise ValueError('changed-since needs a local cursor and a map of host cursors')
    host_cursors = bookmark.get('hosts', {})
    if not isinstance(host_cursors, dict):
        raise ValueError('invalid lifecycle host cursors')
    cursors = [bookmark.get('local', 0), *host_cursors.values()]
    if any(type(c) is not int or not 0 <= c < 2**64 for c in cursors):
        raise ValueError('invalid lifecycle cursor')

    def decode(payload, cursor):
        page = json.loads(payload)
        rows = page['rows']
        if (not isinstance(rows, list) or len(rows) > 100 or
                any(type(row.get('seq')) is not int or row['seq'] != cursor + i
                    for i, row in enumerate(rows, 1)) or
                page['next_cursor'] != cursor + len(rows) or type(page['more']) is not bool):
            raise ValueError('invalid lifecycle page')
        return page

    local = bookmark.get('local', 0)
    local_page = decode(lifecycle_command(state, 'runs', '--changed-since', str(local),
                                          stdout=subprocess.PIPE).stdout, local)

    def fetch(host):
        cursor = host_cursors.get(host['name'], 0)
        try:
            if host.get('invalid'):
                raise ValueError('invalid host')
            return decode(transport(dict(host, lifecycle_cursor=cursor), TIMEOUT), cursor)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
            return {'error': 'host lifecycle query failed'}

    hosts = configured_hosts()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        pages = dict(zip((host['name'] for host in hosts), pool.map(fetch, hosts)))
    next_hosts = dict(host_cursors)
    next_hosts.update({name: page['next_cursor'] for name, page in pages.items() if 'next_cursor' in page})
    return {'local': local_page, 'hosts': pages,
            'next_cursor': {'local': local_page['next_cursor'], 'hosts': next_hosts}}


def native_receipt(record):
    """A target PID is evidence, never an origin-side process identity."""
    if record.get('overflow_remote') and (record.get('run_kind') == 'native-codex' or record.get('role') == 'review'):
        record['origin_receipt'] = True
        identity = {key: record.pop(key) for key in list(record)
                    if key in ('pid', 'pid_start', 'job_id', 'job_name', 'escapees')
                    or key.startswith('supervisor_')}
        if identity:
            record['remote_process'] = dict(record.get('remote_process') or {}, **identity)
    return record


# Verbs whose text travels on standard input to `pg-spawnd <verb>-local` on the owning host.
STDIN_VERBS = ('steer', 'resume')
_RECEIPT_FLAG = dict(steer='steered', resume='resumed')


def native_result(record, *, verb='result', message=None):
    """Refresh a native overflow receipt with target-host terminal evidence."""
    from plangraph.keeper.credential_guard import mask
    try:
        if verb not in ('result', 'cancel', 'steer', 'resume'):
            raise ValueError('unsupported native control verb')
        from .overflow import placement_config, registry_caps
        host = placement_config(registry_caps())['hosts'][record['host']]
        run_id = record['run_id']
        if not re.fullmatch(r'run-[A-Za-z0-9]+', run_id):
            raise ValueError('invalid run id')
        nonce = secrets.token_hex(16)
        begin, end = (f'PLANGRAPH_RECEIPT_{nonce}_{suffix}' for suffix in ('BEGIN', 'END'))
        command = 'cd ' + shlex.quote(host['repo']) + ' && '
        if host.get('env_file'):
            command += 'set -a && . ' + shlex.quote(host['env_file']) + ' >/dev/null 2>&1 && set +a && '
        command += "printf '%s\\n' " + shlex.quote(begin) + ' && '
        state_args = ['--state', host['state_dir']] if host.get('state_dir') else []
        argv = (['pg-spawnd', f'{verb}-local', run_id, *state_args] if verb in STDIN_VERBS else
                ['python3', '-m', 'plangraph.spawner.cli', *state_args, verb, run_id])
        command += 'PYTHONPATH=' + shlex.quote(host['spawner_src']) + ' ' + shlex.join(argv)
        command += "; printf '\\n%s\\n' " + shlex.quote(end)
        step = {'argv': argv, 'env': {'PYTHONPATH': host['spawner_src']},
                'receipt': {'begin': begin, 'end': end}}
        if verb in STDIN_VERBS:
            step['stdin_text'] = message
        plan = {'cwd': host['repo'], 'steps': [step]}
        if host.get('env_file'):
            plan['env_file'] = host['env_file']
        command, body = invocation(host, command, plan.pop('steps'), **plan)
        if body is None and verb in STDIN_VERBS:
            body = message.encode()
        reply = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
                                '--', host['ssh'], command], capture_output=True, timeout=10,
                               **(dict(text=True, encoding='utf-8', errors='replace') if body is None
                                  else dict(input=body)))
        if body is not None:
            reply.stdout = reply.stdout.decode('utf-8', errors='replace')
        # A terminal nonzero exit is the writer's exit, with a valid JSON record.
        text, _ = mask(reply.stdout)
        lines = text.splitlines()
        if lines.count(begin) != 1 or lines.count(end) != 1 or lines.index(begin) >= lines.index(end):
            raise ValueError('receipt unbracketed')
        remote = json.loads('\n'.join(lines[lines.index(begin) + 1:lines.index(end)]))
        if not isinstance(remote, dict):
            raise ValueError('invalid receipt')
        if verb in STDIN_VERBS:
            if remote.get('run_id') != run_id or type(remote.get('ok')) is not bool:
                raise ValueError(f'{verb} receipt mismatch')
            return remote
        if (remote.get('run_id') != run_id or remote.get('host') != record.get('host') or remote.get('run_kind') != record.get('run_kind')
                or remote.get('role') != record.get('role')):
            raise ValueError('receipt mismatch')
        return native_receipt(dict(remote, host=record['host'], overflow_remote=True,
                    overflow_reason=record.get('overflow_reason'), placement=record.get('placement')))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        if verb in STDIN_VERBS:
            return dict(run_id=record['run_id'], ok=False, **{_RECEIPT_FLAG[verb]: False},
                        error=f'{verb} host route failed')
        return native_receipt(dict(record, remote_query_error='host query failed'))


def steer_run(service, run_id, message, host=None):
    """Route a steer to its target using the native run receipt or named host."""
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 16384 or '\0' in message:
        return dict(run_id=run_id, ok=False, steered=False,
                    error='steer message must contain between 1 byte and 16 KiB of text')
    return _route_native(service, run_id, message, host, 'steer')


def resume_run(service, run_id, prompt, host=None):
    """Route a fix-round prompt to the host that owns the native run and resume its saved session."""
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 1 << 20 or '\0' in prompt:
        return dict(run_id=run_id, ok=False, resumed=False,
                    error='resume prompt must contain between 1 byte and 1 MiB of text')
    return _route_native(service, run_id, prompt, host, 'resume')


def _route_native(service, run_id, message, host, verb):
    from plangraph.fleet_snapshot import _projection_binary
    flag = _RECEIPT_FLAG[verb]
    refused = lambda reason: {'run_id': run_id, 'ok': False, flag: False, 'error': reason}
    if not isinstance(run_id, str) or not re.fullmatch(r'run-[A-Za-z0-9]{1,64}', run_id):
        return refused('unknown run id')
    try:
        record = service.result(run_id)
    except ValueError:
        record = None
    if record and not (record.get('overflow_remote') or record.get('origin_receipt')) and host in (None, record.get('host')):
        reply = subprocess.run([_projection_binary(), f'{verb}-local', run_id, '--state', str(service.state)],
                               input=message.encode(), capture_output=True, timeout=10)
        if reply.returncode:
            return refused(f'local {verb} command failed')
        try:
            return json.loads(reply.stdout)
        except ValueError:
            return refused(f'local {verb} command returned no receipt')
    if host is None and record:
        host = record.get('host')
    if host is None:
        hosts = {row['host'] for row in list_runs([]) if row.get('run_id') == run_id}
        if len(hosts) != 1:
            return refused(f'{verb} host is ambiguous' if hosts else 'unknown run id')
        host = hosts.pop()
    return native_result(dict(run_id=run_id, host=host), verb=verb, message=message)


def collect_local(state):
    # Local-only protocol endpoint: never invoke ps (which would fan out again).
    from .stall_watchdog import read_runs
    return {'runs': [{k: r[k] for k in FIELDS if k in r} for r in read_runs(state)
                     if not is_terminal(r.get('status'))]}


if __name__ == '__main__':
    from .service import SpawnService
    print(json.dumps(collect_local(SpawnService().state)))
