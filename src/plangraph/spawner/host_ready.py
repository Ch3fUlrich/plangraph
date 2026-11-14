"""Durable, bounded host readiness: python -m plangraph.spawner.host_ready."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import math
import subprocess
import sys
import time

from .doctor import clean
from .host_ready_source import check_sources
from .private_io import atomic_write_private

HOST_TIMEOUT = 120
MAX_AGE = 600


def shared_cargo_target():
    return Path(os.environ.get('PLANGRAPH_CARGO_TARGET_DIR',
                               '~/.cache/plangraph/cargo-target')).expanduser().absolute()


def cargo_child_env(env):
    return dict(env, CARGO_TARGET_DIR=str(shared_cargo_target()))


def disk_free_gb(path):
    path = Path(path).expanduser().absolute()
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / (1024 ** 3)


def cargo_target_size_gb(path):
    # Do not follow links into another cache or repository.
    total = 0
    if not path.exists():
        return 0
    def unreadable(exc):
        if not isinstance(exc, FileNotFoundError):
            raise exc
    for root, dirs, files in os.walk(path, onerror=unreadable):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            entry = Path(root) / name
            if not entry.is_symlink():
                try:
                    total += entry.stat().st_size
                except FileNotFoundError:
                    pass  # Cargo renames and unlinks temporary files during builds.
    return total / (1024 ** 3)


def _windows_cargo_lease(path, exclusive):
    # Shared handles admit concurrent readers; an exclusive cleanup handle
    # cannot open while any run holds a read handle. Closing releases the lease.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0xC0000000 if exclusive else 0x80000000,
                    0 if exclusive else 1, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, 'rb')


def cargo_lease(exclusive=False):
    target = shared_cargo_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    path = target.with_name(target.name + '.plangraph-lock')
    if os.name == 'nt':
        return _windows_cargo_lease(path, exclusive)
    lease = path.open('a+b')
    try:
        import fcntl
        fcntl.flock(lease, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        return lease
    except BaseException:
        lease.close()
        raise


def local_disk_refusal(worktree, service):
    """Check the worktree filesystem and bound the host's shared build cache."""
    try:
        floor = float(os.environ.get('PLANGRAPH_DISK_FLOOR_GB', '5'))
        cap = float(os.environ.get('PLANGRAPH_CARGO_TARGET_CAP_GB', '12'))
        if not all(math.isfinite(v) and v >= 0 for v in (floor, cap)):
            raise ValueError('invalid disk limits')
        target = shared_cargo_target()
        checkout, cache = Path(worktree).resolve(), target.resolve()
        if (target.is_symlink() or checkout.is_relative_to(cache) or
                cache.is_relative_to(checkout) or cache == Path.home().resolve()):
            raise ValueError('unsafe cache directory')
        size = cargo_target_size_gb(target)
        if size > cap:
            from .runner import is_terminal
            if any(r.get('survivors') or (r.get('status') != 'queued' and not is_terminal(r.get('status')))
                   for r in service.ps()['runs']):
                return f'cargo_target_cap: measured {size:.2f} GB, cap {cap:g} GB; busy'
            try:
                with cargo_lease(exclusive=True):
                    if target.exists():
                        shutil.rmtree(target)
            except OSError:
                return f'cargo_target_cap: measured {size:.2f} GB, cap {cap:g} GB; busy or cleanup unavailable'
        paths = [worktree]
        if getattr(service, 'state', None) is not None:
            from plangraph.settings import _policy_data
            policy = _policy_data(worktree) or {}
            paths.append(policy.get('worktree_root') or service.state / 'worktrees')
            paths.append(service.state / 'trees')
        free = min(disk_free_gb(p) for p in paths)
        if free < floor:
            return f'disk_low: free {free:.2f} GB, floor {floor:g} GB'
    except (OSError, ValueError, TypeError, KeyError):
        return 'disk_low: measurement or configuration unavailable'
    return None


def fresh(record, now=None):
    """Consumers must reject stale readiness, even if the saved check passed."""
    try:
        age = ((now or datetime.now(timezone.utc)) -
               datetime.fromisoformat(record['checked_at'])).total_seconds()
        return record['ok'] is True and 0 <= age <= MAX_AGE
    except (KeyError, TypeError, ValueError):
        return False


def check_host(name, config, state, runner=subprocess.run, timeout=HOST_TIMEOUT):
    record = dict(host=name, ok=False, checked_at=None, failures=[], reachable=False,
                  mem_available_mb=None, disk_free_gb=None, checkout_sha=None,
                  main_sha=None, doctor_ok=None, keeper_ok=None,
                  spawner_session=None, live_runs=None, behind_main=None,
                  src_ok=None)
    deadline = time.monotonic() + timeout
    reachability_failed = False

    def fail(check, reason):
        record['failures'].append(clean(f'{check}: {reason}'))

    def command(check, script):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail(check, 'timeout')
            return None
        try:
            reply = runner(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                            '--', config['ssh'], script], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=remaining)
            if reply.returncode and not (check == 'doctor' and reply.returncode == 1):
                fail(check, 'command_failed')
                return None
            return reply.stdout
        except subprocess.TimeoutExpired:
            fail(check, 'timeout')
        except OSError:
            fail(check, 'command_failed')
        return None

    def measure(check, script, parse):
        output = command(check, script)
        if output is not None:
            try:
                return parse(output)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                fail(check, 'invalid_response')
        return None

    try:
        if not config['ssh']:
            fail('reachable', 'no_route')
            reachability_failed = True
        elif command('reachable', 'true') is not None:
            record['reachable'] = True
            record['src_ok'] = check_sources(name, config, command, fail)
            repo = shlex.quote(config['repo'])
            prefix = 'cd ' + shlex.quote(config['spawner_src']) + ' && '
            if config.get('env_file'):
                prefix += 'set -a && . ' + shlex.quote(config['env_file']) + ' >/dev/null 2>&1 && '
            prefix += 'PYTHONPATH=src python3 -m plangraph.spawner.'
            record['mem_available_mb'] = measure('memory', 'cat /proc/meminfo',
                lambda s: int(re.search(r'^MemAvailable:\s+(\d+) kB$', s, re.M)[1]) // 1024)
            record['disk_free_gb'] = measure('disk', 'df -Pk -- ' + repo,
                lambda s: int(s.splitlines()[-1].split()[3]) // (1024 * 1024))
            def sha(s):
                value = s.strip()
                if not re.fullmatch(r'[0-9a-f]{40,64}', value):
                    raise ValueError()
                return value
            record['checkout_sha'] = measure('checkout', 'git -C ' + repo + ' rev-parse HEAD', sha)
            fetched = command('fetch', 'git -C ' + repo + ' fetch origin main')
            if fetched is not None:
                record['main_sha'] = measure('main', 'git -C ' + repo + ' rev-parse origin/main', sha)
                def count(s):
                    if not re.fullmatch(r'\d+', s.strip()):
                        raise ValueError()
                    return int(s.strip())
                record['behind_main'] = measure('behind_main', 'git -C ' + repo +
                    ' rev-list --count HEAD..origin/main', count)
                if record['behind_main']:
                    fail('behind_main', str(record['behind_main']) + ' behind')
            if record['checkout_sha'] and record['main_sha'] and record['checkout_sha'] != record['main_sha']:
                fail('checkout', 'behind')
            def parse_doctor(output):
                doctor = json.loads(output)
                ok, rows = doctor['ok'], doctor['checks']
                if not isinstance(ok, bool) or not isinstance(rows, list):
                    raise ValueError()
                for row in rows:
                    if not isinstance(row['name'], str) or row['status'] not in ('pass', 'warn', 'fail'):
                        raise ValueError()
                return ok, rows
            doctor = measure('doctor', prefix + 'doctor --json', parse_doctor)
            if doctor is not None:
                ok, rows = doctor
                record['doctor_ok'] = ok and not any(r['status'] == 'fail' for r in rows)
                record['keeper_ok'] = any(r['name'] == 'keeper_whoami' and r['status'] == 'pass' for r in rows)
                for row in rows:
                    if row['status'] == 'fail':
                        label = row['name'] if re.fullmatch(r'[a-z_]+', row['name']) else 'check'
                        fail('doctor.' + label, 'failed')
                if not record['doctor_ok']:
                    fail('doctor', 'failed')
                if not record['keeper_ok']:
                    fail('keeper', 'whoami_failed')
            if config.get('session_unit'):
                session = config['session_unit']
                probe = ('systemctl --user is-active --quiet -- ' if session.endswith(
                    ('.service', '.scope', '.socket', '.target', '.timer', '.path',
                     '.mount', '.automount', '.swap', '.slice', '.device')) else 'pgrep -f -- ')
                record['spawner_session'] = command('session', probe + shlex.quote(session)) is not None
            else:
                fail('session', 'not_configured')
            runs = measure('runs', prefix + 'cli ps', lambda s: json.loads(s)['runs'])
            if runs is not None:
                record['live_runs'] = sum(r['status'] == 'running' for r in runs)
            if record['mem_available_mb'] is not None and record['mem_available_mb'] < config['floor_mb']:
                fail('memory', 'below_floor')
            if record['disk_free_gb'] is not None and record['disk_free_gb'] < config.get('disk_floor_gb', 5):
                fail('disk', f"disk_low: free {record['disk_free_gb']} GB")
        else:
            reachability_failed = True
    except (MemoryError, RecursionError):
        raise
    except Exception:
        # Ordinary host/probe errors must not hide any host's lines.
        fail('config', 'invalid_response_or_config')
    record['checked_at'] = datetime.now(timezone.utc).isoformat()
    if reachability_failed:
        fail('unreachable', 'other_checks_unknown')
    record['ok'] = not record['failures']
    try:
        atomic_write_private(Path(state) / (name + '.json'), json.dumps(record))
    except (MemoryError, RecursionError):
        raise
    except Exception:
        # Persistence errors also leave a reportable failure, never abort the fan-out.
        fail('state', 'write_failed')
        record['ok'] = False
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    config = Path(os.environ.get('PLANGRAPH_OVERFLOW_HOSTS',
                                Path.home() / '.config/plangraph/overflow-hosts.json'))
    hosts = json.loads(config.read_text())['hosts']
    if args.host:
        if args.host not in hosts:
            parser.error('unknown host')
        hosts = {args.host: hosts[args.host]}
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in hosts):
        parser.error('invalid host name')
    state = Path.home() / '.local/state/plangraph/host-ready'
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # Each host reports its own guarded write failure below.
    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
        futures = [pool.submit(check_host, name, cfg, state) for name, cfg in hosts.items()]
        records = [future.result() for future in futures]
    if args.json:
        print(json.dumps(records))
    else:
        for record in records:
            print(record['host'] + (' ok' if record['ok'] else ' FAIL ' + '; '.join(record['failures'])))
    for record in records:
        for failure in record['failures']:
            check, _, reason = failure.partition(': ')
            print(f"HOST-NOT-READY {record['host']} {check} {reason}",
                  file=sys.stderr if args.json else sys.stdout)
    return int(any(not r['ok'] for r in records))


if __name__ == '__main__':
    raise SystemExit(main())
