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
# Mirrors pg_worker_core::cargo_pool::DEFAULT_CARGO_TARGET_CAP_GB (a test pins both).
DEFAULT_CARGO_TARGET_CAP_GB = 20


def cargo_target_cap_gb():
    return float(os.environ.get('PLANGRAPH_CARGO_TARGET_CAP_GB', str(DEFAULT_CARGO_TARGET_CAP_GB)))


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


def _legacy_cache_roots():
    """Per-user caches of earlier layouts, held under the same cap as the shared target."""
    return [(Path.home() / rel).expanduser().absolute()
            for rel in ('.cache/pg-target-po', '.cache/pg-target-warm')]


def _pool_slot_roots(primary):
    """Pool slots beside the primary target (`<primary>-pool/slot-N`), as created by cargo_pool."""
    pool = primary.with_name(primary.name + '-pool')
    try:
        return sorted(p for p in pool.glob('slot-*')
                      if p.is_dir() and not p.is_symlink() and '.' not in p.name)
    except OSError:
        return []


def _shared_cache_roots():
    primary = shared_cargo_target()
    roots = [primary]
    for extra in _legacy_cache_roots() + _pool_slot_roots(primary):
        if extra not in roots:
            roots.append(extra)
    return roots


def _clean_single_cache(target, service, cap=None):
    """Remove an idle, unleased shared cache dir. Returns True if cleaned."""
    if not target.exists() or target.is_symlink():
        return False
    if cap is not None and cargo_target_size_gb(target) < cap:
        return False
    try:
        wait = float(os.environ.get(CLEANUP_WAIT_ENV, '30'))
        with _cargo_lease_for(target, exclusive=True, wait=wait if math.isfinite(wait) and wait >= 0 else 30.0):
            if any(cargo_run_busy(r) for r in service.ps()['runs']):
                return False
            if target.exists():
                if cap is not None and cargo_target_size_gb(target) < cap:
                    return True  # A competing cleanup already made room.
                shutil.rmtree(target)
                return True
    except OSError:
        return False
    return False


def _cleanup_before_disk_refusal(worktree, service):
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
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
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


def select_work_drive(drives, cleanup):
    """The one drive decision: the first configured drive at or above its floor.

    ``drives`` is ``[(path, floor_gb)]``. When none qualifies, ``cleanup`` runs once
    and every drive is measured again. Returns ``(path, None)`` for the selected
    drive, or ``(None, (free_gb, floor_gb))`` of the first drive when all stay low.
    """
    if not drives:
        raise ValueError('select_work_drive needs at least one drive')

    def pick():
        return next((p for p, floor in drives if disk_free_gb(p) >= floor), None)
    selected = pick()
    if selected is None:
        cleanup()
        selected = pick()
    if selected is not None:
        return selected, None
    first_path, first_floor = drives[0]
    return None, (disk_free_gb(first_path), first_floor)


#: Roles that build nothing, so the shared build cache's size cap does not bind them.
NO_BUILD_ROLES = ('review',)


def local_disk_refusal(worktree, service, role='implement'):
    """Check the worktree filesystem and bound the host's shared build cache.

    A role in NO_BUILD_ROLES keeps the free-space floor but not the cache cap.
    """
    # SettingsError derives from ValueError: validate before measurement's catch.
    from plangraph.settings import _policy_data, SettingsError, as_spawn_refused
    try:
        policy = _policy_data(worktree) or {}
    except SettingsError as exc:
        raise as_spawn_refused(exc) from None
    try:
        floor = float(os.environ.get('PLANGRAPH_DISK_FLOOR_GB', '5'))
        cap = cargo_target_cap_gb()
        if not all(math.isfinite(v) and v >= 0 for v in (floor, cap)):
            raise ValueError('invalid disk limits')
        target = shared_cargo_target()
        if not _safe_cache_dir(target, worktree):
            raise ValueError('unsafe cache directory')
        building = role not in NO_BUILD_ROLES
        size = cargo_target_size_gb(target) if building else 0
        if building and size >= cap:
            if not _clean_single_cache(target, service, cap=cap):
                return f'cargo_target_cap: measured {size:.2f} GB in {target}, cap {cap:g} GB; busy or cleanup unavailable'
        if building:
            # Hold the legacy caches and the pool slots under the same cap.
            for extra in _legacy_cache_roots() + _pool_slot_roots(target):
                if extra != target and _safe_cache_dir(extra, worktree) and cargo_target_size_gb(extra) >= cap:
                    _clean_single_cache(extra, service, cap=cap)
        if cap == 0 and building:
            return f'cargo_target_cap: cap 0 GB for {target}; no build capacity'
        drives = _configured_work_drives(policy)
        if drives:
            selected, low = select_work_drive(
                drives, lambda: _cleanup_before_disk_refusal(worktree, service))
            if selected is not None:
                return None
            free, low_floor = low
            reason = f'disk_low: free {free:.2f} GB, floor {low_floor:g} GB'
            _record_disk_floor_alarm(service, free, low_floor, reason)
            return reason
        paths = [worktree, target]
        if getattr(service, 'state', None) is not None:
            paths.append(policy.get('worktree_root') or service.state / 'worktrees')
            paths.append(service.state / 'trees')
        free = min(disk_free_gb(p) for p in paths)
        if free < floor:
            _cleanup_before_disk_refusal(worktree, service)
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

    Emergency disk pressure can stop work. The cache cap is an admission limit,
    not a reason to interrupt work that already holds a lease.
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
    """Rule 7.1: stop below 2 GiB, irrespective of cache size."""
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
    except (OSError, ValueError, TypeError, KeyError):
        return 'disk_low: runtime measurement or configuration unavailable'
    return None


def cargo_cap_refusal():
    """A refusal when the shared cache is at or over its cap, for a standalone build that
    holds the shared lease (push preflight). The spawner's own runs are never stopped for
    cache size, so `running_disk_refusal` does not apply it."""
    try:
        cap = cargo_target_cap_gb()
        if not math.isfinite(cap) or cap < 0:
            raise ValueError('invalid cache cap')
        target = shared_cargo_target()
        size = cargo_target_size_gb(target)
        if size >= cap:
            return f'cargo_target_cap: measured {size:.2f} GB in {target}, cap {cap:g} GB'
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


VENV_PYTHONS = ('.venv/bin/python', '.venv/Scripts/python.exe')


def pre_push_report(repo, unit=None, environ=None):
    """What a pre-push run needs from this host, for `host_ready --test-tools` on the target.

    The interpreter is the hook's choice (tools/hooks/pre-push): the checkout venv, else
    PREFLIGHT_PYTHON (home-expanded, relative to the checkout), else python3. Files created
    under a umask that leaves group or other write set fail the record-permission checks,
    so this process's umask and the user service's UMask must keep both bits.
    """
    environ = os.environ if environ is None else environ
    root = Path(repo).absolute()
    venv = next((root / rel for rel in VENV_PYTHONS if (root / rel).is_file()), None)
    configured = environ.get('PREFLIGHT_PYTHON')
    if venv is not None:
        interpreter = str(venv.absolute())
    elif configured:
        path = Path(configured).expanduser()
        interpreter = str(path if path.is_absolute() else root / path)
    else:
        # The hook runs in the checkout: a relative or empty PATH entry means a directory there.
        entries = [entry if os.path.isabs(entry) else str(root / (entry or '.'))
                   for entry in environ.get('PATH', os.defpath).split(os.pathsep)]
        interpreter = shutil.which('python3', path=os.pathsep.join(entries)) or 'python3'
    missing = []
    for module in ('pytest', 'xdist'):
        try:
            found = subprocess.run([interpreter, '-c', 'import ' + module], capture_output=True,
                                   timeout=30, cwd=root).returncode == 0
        except (OSError, subprocess.SubprocessError):
            found = False
        if not found:
            missing.append(module)
    mask = os.umask(0)
    os.umask(mask)
    report = dict(missing=missing, umask=format(mask, '04o'), unit_umask=None)
    if unit:
        try:
            reply = subprocess.run(['systemctl', '--user', 'show', '--property=UMask', '--value', unit],
                                   capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', timeout=10)
            shown = reply.stdout.strip()
            report['unit_umask'] = shown if reply.returncode == 0 and re.fullmatch(r'[0-7]+', shown) else None
        except (OSError, subprocess.SubprocessError):
            pass
    return report


def python_test_tools(measure, fail, prefix, repo, unit=None, spawner=None):
    """Run pre_push_report on the host and report missing pytest/xdist or a writable umask."""
    command = prefix + 'host_ready --test-tools ' + shlex.quote(repo)
    if unit:
        command += ' --unit ' + shlex.quote(unit)
    plan = spawner(command[len(prefix):]) if spawner else {}
    report = measure('python', command, json.loads, **plan)
    if report is None:
        return False
    missing = [m for m in report['missing'] if m in ('pytest', 'xdist')]
    if missing:
        fail('python', 'missing ' + ' '.join(missing))
    if unit and report['unit_umask'] is None:
        fail('umask', f'unit umask of {unit} unavailable')
    for label, value in (('umask', report['umask']), ('unit umask', report['unit_umask'])):
        if value is not None and int(value, 8) & 0o022 != 0o022:
            fail('umask', f'{label} {value} leaves new files group- or other-writable')
    return not missing


def check_host(name, config, state, runner=subprocess.run, timeout=HOST_TIMEOUT):
    record = dict(host=name, ok=False, checked_at=None, failures=[], reachable=False,
                  mem_available_mb=None, disk_free_gb=None, checkout_sha=None,
                  main_sha=None, doctor_ok=None, keeper_ok=None,
                  spawner_session=None, live_runs=None, behind_main=None,
                  src_ok=None, trust_ok=None, tools_ok=None, python_ok=None, tokens_ok=None)
    deadline = time.monotonic() + timeout
    reachability_failed = False

    def fail(check, reason):
        record['failures'].append(clean(f'{check}: {reason}'))

    def command(check, script, steps=None, outputs=None, **plan):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail(check, 'timeout')
            return None
        try:
            from .remote_ps import invocation, plan_body
            if config.get('local'):
                argv, body = ['pg-spawnd', 'steps'], plan_body(script, steps, **plan)
            else:
                remote, body = invocation(config, script, steps, **plan)
                argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        '--', config['ssh'], remote]
            extra = {} if body is None else {'input': body.decode('utf-8')}
            reply = runner(argv, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=remaining, **extra)
            if body is not None and outputs and reply.returncode in outputs:
                return outputs[reply.returncode] + '\n'  # A plan reports its branch as an exit code.
            if reply.returncode and not (check == 'doctor' and reply.returncode == 1):
                fail(check, 'command_failed')
                return None
            return reply.stdout
        except subprocess.TimeoutExpired:
            fail(check, 'timeout')
        except OSError:
            fail(check, 'command_failed')
        return None

    def command_argv(check, *words):
        """Run one argv: arguments with shell-special characters stay literal on every transport."""
        return command(check, shlex.join(words), [{'argv': list(words)}])

    def measure_argv(check, words, parse):
        return measure(check, shlex.join(words), parse, steps=[{'argv': list(words)}])

    def measure(check, script, parse, **plan):
        output = command(check, script, **plan)
        if output is not None:
            try:
                return parse(output)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                fail(check, 'invalid_response')
        return None

    try:
        if not config.get('local') and not config['ssh']:
            fail('reachable', 'no_route')
            reachability_failed = True
        elif command('reachable', 'true') is not None:
            record['reachable'] = True
            record['src_ok'] = check_sources(name, config, command, fail)
            if config.get('registry'):
                record['tools_ok'] = command(
                    'tools', 'command -v git && command -v python3 && command -v codex',
                    [{'argv': [tool, '--version']} for tool in ('git', 'python3', 'codex')]) is not None
                record['tokens_ok'] = command('tokens', 'codex login status') is not None
                record['trust_ok'] = command_argv(
                    'trust', 'pg-host-readiness', '--project', config['repo']) is not None
            repo = shlex.quote(config['repo'])
            prefix = 'cd ' + shlex.quote(config['spawner_src']) + ' && '
            if config.get('env_file'):
                prefix += 'set -a && . ' + shlex.quote(config['env_file']) + ' >/dev/null 2>&1 && '
            if config.get('state_dir'):
                prefix += 'PLANGRAPH_SPAWN_STATE=' + shlex.quote(config['state_dir']) + ' '
            prefix += 'PYTHONPATH=src python3 -m plangraph.spawner.'
            def spawner(rest):
                """The step plan of `python3 -m plangraph.spawner.<rest>` run from the source dir."""
                name, *args = shlex.split(rest)
                env = {'PYTHONPATH': 'src'}
                if config.get('state_dir'):
                    env['PLANGRAPH_SPAWN_STATE'] = config['state_dir']
                plan = dict(cwd=config['spawner_src'], steps=[
                    {'argv': ['python3', '-m', 'plangraph.spawner.' + name, *args], 'env': env}])
                if config.get('env_file'):
                    plan['env_file'] = config['env_file']
                return plan
            if config.get('registry'):
                session = config.get('session_unit') or ''
                record['python_ok'] = python_test_tools(
                    measure, fail, prefix, config['repo'],
                    session if session.endswith('.service') else None, spawner)
            record['mem_available_mb'] = measure('memory', 'cat /proc/meminfo',
                lambda s: int(re.search(r'^MemAvailable:\s+(\d+) kB$', s, re.M)[1]) // 1024)
            record['disk_free_gb'] = measure_argv('disk', ['df', '-Pk', '--', config['repo']],
                lambda s: int(s.splitlines()[-1].split()[3]) // (1024 * 1024))
            def sha(s):
                value = s.strip()
                if not re.fullmatch(r'[0-9a-f]{40,64}', value):
                    raise ValueError()
                return value
            record['checkout_sha'] = measure_argv('checkout', ['git', '-C', config['repo'], 'rev-parse', 'HEAD'], sha)
            fetched = command_argv('fetch', 'git', '-C', config['repo'], 'fetch', 'origin', 'main')
            if fetched is not None:
                record['main_sha'] = measure_argv('main', ['git', '-C', config['repo'], 'rev-parse', 'origin/main'], sha)
                def count(s):
                    if not re.fullmatch(r'\d+', s.strip()):
                        raise ValueError()
                    return int(s.strip())
                record['behind_main'] = measure_argv('behind_main', ['git', '-C', config['repo'], 'rev-list', '--count', 'HEAD..origin/main'],
                    count)
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
            doctor = measure('doctor', prefix + 'doctor --json', parse_doctor, **spawner('doctor --json'))
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
                record['spawner_session'] = command_argv('session', *shlex.split(probe), session) is not None
            elif not config.get('registry'):
                fail('session', 'not_configured')
            run_probe = 'remote_ps' if config.get('registry') else 'cli ps'
            def count_live_runs(output):
                runs = json.loads(output)['runs']
                if not isinstance(runs, list) or any(not isinstance(row['status'], str) for row in runs):
                    raise ValueError()
                return sum(row['status'] == 'running' for row in runs)
            record['live_runs'] = measure('runs', prefix + run_probe, count_live_runs,
                                          **spawner(run_probe))
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
    parser.add_argument('--test-tools', metavar='REPO')
    parser.add_argument('--unit')
    args = parser.parse_args(argv)
    if args.test_tools:
        print(json.dumps(pre_push_report(args.test_tools, args.unit)))
        return 0
    from .overflow import host_config, placement_config, readiness_dir, registry_caps
    registry = registry_caps()
    hosts = placement_config(registry).get('hosts', {})
    local = host_config(registry, os.environ.get('PLANGRAPH_HOST_NAME') or registry['self'])
    if local:
        if local['id'] in hosts:
            hosts[local['id']]['local'] = True
        elif local.get('os') == 'linux' and 'plangraph' in local.get('repos', {}):
            repo = local['repos']['plangraph']['root']
            hosts[local['id']] = dict(local=True, registry=True, repo=repo,
                spawner_src=repo + '/src', floor_mb=local['caps']['mem_floor_mb'],
                state_dir=local.get('state_dir'),
                session_unit=local.get('env', {}).get('PG_SESSION_UNIT'))
    if not hosts and not args.host:
        parser.error('hosts.json has no readiness candidates')
    if args.host:
        if args.host not in hosts:
            parser.error('unknown host')
        hosts = {args.host: hosts[args.host]}
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in hosts):
        parser.error('invalid host name')
    state = readiness_dir()
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
