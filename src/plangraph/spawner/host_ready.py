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
    return dict(env, CARGO_TARGET_DIR=str(shared_cargo_target()), CARGO_INCREMENTAL="0")


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


CLEANUP_WAIT_ENV = 'PLANGRAPH_CARGO_CLEANUP_WAIT_S'


def _safe_cache_dir(target, worktree):
    checkout, cache = Path(worktree).resolve(), target.resolve()
    return not (target.is_symlink() or checkout.is_relative_to(cache) or
                cache.is_relative_to(checkout) or cache == Path.home().resolve())


def _shared_cache_roots():
    primary = shared_cargo_target()
    roots = [primary]
    for rel in ('.cache/pg-target-po', '.cache/pg-target-warm'):
        extra = (Path.home() / rel).expanduser().absolute()
        if extra not in roots:
            roots.append(extra)
    return roots


def _clean_single_cache(target, service, cap=None):
    """Remove an idle, unleased shared cache dir. Returns True if cleaned."""
    if not target.exists() or target.is_symlink():
        return False
    if cap is not None and cargo_target_size_gb(target) < cap:
        return False
    if any(cargo_run_busy(r) for r in service.ps()['runs']):
        return False
    try:
        wait = float(os.environ.get(CLEANUP_WAIT_ENV, '30'))
        with _cargo_lease_for(target, exclusive=True, wait=wait if math.isfinite(wait) and wait >= 0 else 30.0):
            if target.exists():
                shutil.rmtree(target)
                return True
    except OSError:
        return False
    return False


def _cleanup_before_disk_refusal(worktree, service, cap):
    """Reclaim idle rebuildable build caches before refusing disk_low."""
    cleaned = False
    for root in _shared_cache_roots():
        if _safe_cache_dir(root, worktree) and _clean_single_cache(root, service):
            cleaned = True
    try:
        wt_path = Path(worktree)
        if wt_path.is_dir() and not any(cargo_run_busy(r) for r in service.ps()['runs']):
            proc = subprocess.run(
                ['git', '-C', str(wt_path), 'worktree', 'list', '--porcelain'],
                capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                live_cwds = {Path(r['cwd']).resolve() for r in service.ps()['runs']
                             if cargo_run_busy(r) and r.get('cwd')}
                for line in proc.stdout.splitlines():
                    if not line.startswith('worktree '):
                        continue
                    tree = Path(line[len('worktree '):])
                    if not tree.is_dir() or tree.is_symlink():
                        continue
                    if any(tree.resolve() == c or tree.resolve().is_relative_to(c) for c in live_cwds):
                        continue
                    rust_target = tree / 'rust/target'
                    if rust_target.is_dir() and not rust_target.is_symlink():
                        try:
                            with _cargo_lease_for(rust_target, exclusive=True, wait=0.0):
                                shutil.rmtree(rust_target)
                                cleaned = True
                        except OSError:
                            pass
    except (OSError, subprocess.SubprocessError):
        pass
    return cleaned


def _record_disk_floor_alarm(service, free_gb, floor_gb, reason):
    """Persist disk-hygiene.json alarm so heartbeat and keeper observe floor breaches."""
    state = getattr(service, 'state', None)
    if state is None:
        return
    state = Path(state)
    try:
        state.mkdir(parents=True, exist_ok=True)
        payload = dict(
            alarm=True,
            free_bytes=max(0, int(free_gb * (1024 ** 3))),
            floor_bytes=max(0, int(floor_gb * (1024 ** 3))),
            dry_run=False,
            freed_bytes=0,
            actions=[],
            skipped=[],
            error=reason,
            ts=int(time.time()),
        )
        raw = json.dumps(payload)
        atomic_write_private(state / 'disk-hygiene.json', raw)
        with (state / 'disk-hygiene.ndjson').open('a', encoding='utf-8') as f:
            f.write(raw + '\n')
    except OSError:
        pass


def _cargo_lease_for(target, exclusive=False, wait=0.0):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    path = target.with_name(target.name + '.plangraph-lock')
    deadline = time.monotonic() + wait
    while True:
        try:
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
        except OSError:
            if not exclusive or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.5, max(deadline - time.monotonic(), 0)))


def cargo_lease(exclusive=False, wait=0.0):
    """Shared lease for a build; exclusive for cleanup, which waits up to ``wait`` seconds for live builds."""
    return _cargo_lease_for(shared_cargo_target(), exclusive=exclusive, wait=wait)


def cargo_run_busy(record):
    """A run keeps the shared cargo target busy only while one of its processes can still build.

    Ended, timed_out, stalled and queued records, and records whose recorded pids are all gone,
    do not; the exclusive lease taken by the cleanup is the final guard against a live build."""
    from .runner import is_terminal
    from .exit_outcome import run_pids_dead
    if record.get('survivors'):
        return True
    status = record.get('status')
    if is_terminal(status) or status in ('queued', 'timed_out', 'stalled') or record.get('ended') is not None:
        return False
    return not run_pids_dead(record)


def _configured_work_drives(policy):
    raw = os.environ.get('PLANGRAPH_WORK_DRIVES')
    drives = json.loads(raw) if raw else policy.get('work_drives')
    if not drives:
        return None
    if not isinstance(drives, list):
        raise ValueError('work_drives must be a list')
    parsed = []
    for entry in drives:
        if not isinstance(entry, dict) or not entry.get('path'):
            raise ValueError('invalid work_drive entry')
        drive_floor = float(entry.get('disk_floor_gb', entry.get('floor_gb', 5)))
        if not math.isfinite(drive_floor) or drive_floor < 0:
            raise ValueError('invalid work_drive floor')
        parsed.append((entry['path'], drive_floor))
    return parsed


def local_disk_refusal(worktree, service):
    """Check the worktree filesystem and bound the host's shared build cache."""
    # SettingsError derives from ValueError: validate before measurement's catch.
    from plangraph.settings import _policy_data, SettingsError, as_spawn_refused
    try:
        policy = _policy_data(worktree) or {}
    except SettingsError as exc:
        raise as_spawn_refused(exc) from None
    try:
        floor = float(os.environ.get('PLANGRAPH_DISK_FLOOR_GB', '5'))
        cap = float(os.environ.get('PLANGRAPH_CARGO_TARGET_CAP_GB', '12'))
        if not all(math.isfinite(v) and v >= 0 for v in (floor, cap)):
            raise ValueError('invalid disk limits')
        target = shared_cargo_target()
        if not _safe_cache_dir(target, worktree):
            raise ValueError('unsafe cache directory')
        size = cargo_target_size_gb(target)
        if size >= cap:
            if any(cargo_run_busy(r) for r in service.ps()['runs']):
                return f'cargo_target_cap: measured {size:.2f} GB, cap {cap:g} GB; busy'
            if not _clean_single_cache(target, service, cap=cap):
                return f'cargo_target_cap: measured {size:.2f} GB, cap {cap:g} GB; busy or cleanup unavailable'
        # Hold legacy plan-order shared target under the same cap.
        po_target = (Path.home() / '.cache/pg-target-po').expanduser().absolute()
        if po_target != target and _safe_cache_dir(po_target, worktree) and cargo_target_size_gb(po_target) >= cap:
            _clean_single_cache(po_target, service, cap=cap)
        if cap == 0:
            return 'cargo_target_cap: cap 0 GB; no build capacity'
        drives = _configured_work_drives(policy)
        if drives:
            ok_drives = [p for p, df in drives if disk_free_gb(p) >= df]
            if not ok_drives:
                _cleanup_before_disk_refusal(worktree, service, cap)
                ok_drives = [p for p, df in drives if disk_free_gb(p) >= df]
            if not ok_drives:
                low_path, low_floor = drives[0]
                free = disk_free_gb(low_path)
                reason = f'disk_low: free {free:.2f} GB, floor {low_floor:g} GB'
                _record_disk_floor_alarm(service, free, low_floor, reason)
                return reason
            return None
        paths = [worktree, target]
        if getattr(service, 'state', None) is not None:
            paths.append(policy.get('worktree_root') or service.state / 'worktrees')
            paths.append(service.state / 'trees')
        free = min(disk_free_gb(p) for p in paths)
        if free < floor:
            _cleanup_before_disk_refusal(worktree, service, cap)
            free = min(disk_free_gb(p) for p in paths)
            if free < floor:
                reason = f'disk_low: free {free:.2f} GB, floor {floor:g} GB'
                _record_disk_floor_alarm(service, free, floor, reason)
                return reason
    except (OSError, ValueError, TypeError, KeyError):
        return 'disk_low: measurement or configuration unavailable'
    return None


async def collect_with_disk_guard(runner, job_id, proc):
    """Sample live disk independently of output and the progress watchdog cadence.

    Never delete a building cache. Over-cap runs are reaped; admission can reclaim
    the cache only after every process lease has been released.
    """
    import asyncio
    from .run_watchdog import collect
    job = runner.jobs[job_id]
    owner, interrupted, reaping, collecting = asyncio.current_task(), False, False, True
    async def watch():
        nonlocal interrupted, reaping
        try:
            while True:
                await asyncio.sleep(1)
                reason = await asyncio.to_thread(running_disk_refusal, job.cwd or runner.default_cwd)
                if reason and job.status not in ('cancelled', 'timeout', 'stalled'):
                    job.refusal = reason
                    job.status, job.exit_code = 'failed', 7
                    job.events.append(dict(event='disk_run_stopped', reason=reason))
                    reaping = True
                    await runner._reap_process(job_id, proc)
                    if job.survivors and collecting:
                        interrupted = True
                        owner.cancel()
                    return
        except Exception:
            owner.cancel()
            raise
    guard = asyncio.create_task(watch())
    try:
        # Await directly: an extra task here delays stdin delivery to fast children.
        return await collect(runner, job_id, proc)
    except asyncio.CancelledError:
        if not interrupted:
            raise
        transport = getattr(proc, '_transport', None)
        for fd in (0, 1, 2):
            pipe = transport.get_pipe_transport(fd) if transport is not None else None
            if pipe is not None:
                pipe.close()  # Never signal an unproven survivor.
        return b'', b''
    finally:
        collecting = False
        if not reaping:
            guard.cancel()
        result, = await asyncio.gather(guard, return_exceptions=True)
        if isinstance(result, Exception):
            raise result


def running_disk_refusal(worktree):
    """Rule 7.1: stop below 2 GiB; cap the shared cache throughout a run."""
    try:
        from plangraph.settings import _policy_data
        policy = _policy_data(worktree) or {}
        target = shared_cargo_target()
        paths = [worktree, target]
        if policy.get('worktree_root'):
            paths.append(policy['worktree_root'])
        free = min(disk_free_gb(path) for path in paths)
        if free < 2:
            return f'disk_low: free {free:.2f} GB, stop 2 GB'
        cap = float(os.environ.get('PLANGRAPH_CARGO_TARGET_CAP_GB', '12'))
        if not math.isfinite(cap) or cap < 0:
            raise ValueError('invalid cache cap')
        size = cargo_target_size_gb(target)
        if size >= cap:
            return f'cargo_target_cap: measured {size:.2f} GB, cap {cap:g} GB'
    except (OSError, ValueError, TypeError, KeyError):
        return 'disk_low: runtime measurement or configuration unavailable'
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
                probe = ('systemctl --user is-active --quiet -- ' if session.endswith((
                    '.service', '.scope', '.socket', '.target', '.timer', '.path', '.mount',
                    '.automount', '.swap', '.slice', '.device')) else
                    'plangraph wait-pid --check --pid-file ' if session.endswith('.pid') else 'pgrep -x -- ')
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
