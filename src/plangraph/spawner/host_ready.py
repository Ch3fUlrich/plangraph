"""Durable, bounded host readiness: python -m plangraph.spawner.host_ready."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import math
import subprocess
import sys
import time

from .private_io import atomic_write_private
from .host_ready_source import (
    checkout_floor, admission_shortfall, work_drive_checkouts,
    configured_build_target, configured_work_drives as _configured_work_drives,
    select_drive,
    legacy_cache_roots as _legacy_cache_roots,
    pool_slot_roots as _pool_slot_roots,
    record_disk_floor_alarm as _record_disk_floor_alarm,
    windows_cargo_lease as _windows_cargo_lease,
    HOST_TIMEOUT, probe_host, python_test_tools,
)

MAX_AGE = 600
_sccache_missing_warned = False
# Mirrors pg_worker_core::cargo_pool::DEFAULT_CARGO_TARGET_CAP_GB (a test pins both).
DEFAULT_CARGO_TARGET_CAP_GB = 32


def _node_version(entry):
    version = re.fullmatch(r'v([0-9]+)\.([0-9]+)\.([0-9]+)(-.+)?', entry.name)
    if not version:
        return (), False, entry.name
    return tuple(map(int, version.groups()[:3])), version[4] is None, entry.name


def harness_path(env, *, native_writer=False):
    """Find user-installed tools for systemd, a bare PATH, or native Codex admission."""
    inherited = env.get('PATH', os.defpath)
    system_dirs = {'/usr/local/sbin', '/usr/local/bin', '/usr/sbin', '/usr/bin', '/sbin', '/bin'}
    path = inherited.split(os.pathsep)
    if os.name != 'posix':
        return inherited
    custom_path = any(entry and entry not in system_dirs for entry in path)
    if custom_path and not native_writer and not env.get('INVOCATION_ID'):
        return inherited
    if custom_path and native_writer and shutil.which('codex', path=inherited):
        return inherited
    home = Path(env.get('HOME') or Path.home())
    versions = Path(env.get('NVM_DIR') or home / '.nvm') / 'versions/node'
    try:
        node = [entry / 'bin' for entry in sorted(
            (entry for entry in versions.iterdir() if not entry.name.startswith('.')),
            key=_node_version, reverse=True)]
    except OSError:
        node = []
    installed = [home / '.local/bin', *node, home / '.qoder/entry']
    added = [str(entry) for entry in installed if entry.is_dir() and str(entry) not in path]
    if custom_path and not native_writer:
        # A service may inherit manager additions or an EnvironmentFile PATH.
        # Keep the inherited lookup order and fill in the user installs.
        path.extend(added)
    else:
        system_start = next((i for i, entry in enumerate(path) if entry in system_dirs), len(path))
        path[system_start:system_start] = added
    if native_writer and not shutil.which('cargo', path=os.pathsep.join(path)):
        cargo_bin = home / '.cargo/bin'
        if cargo_bin.is_dir() and str(cargo_bin) not in path:
            path.append(str(cargo_bin))
    return os.pathsep.join(path)


def shared_cargo_target(policy=None, *, floors=True):
    """The env target, else the `build_drives` target of the settings policy, else the cache default."""
    if not os.environ.get('PLANGRAPH_CARGO_TARGET_DIR'):
        if policy is None:
            from plangraph.settings import _policy_data
            try:
                policy = _policy_data(None) or {}
            except ValueError:
                policy = {}
        if (built := configured_build_target(policy, disk_free_gb, floors=floors)) is not None:
            return built
    return Path(os.environ.get('PLANGRAPH_CARGO_TARGET_DIR',
                               '~/.cache/plangraph/cargo-target')).expanduser().absolute()


def _target_for(policy, floors=True):
    """The target under the checkout's own policy, not the daemon's working directory."""
    if policy.get('build_drives'):
        return shared_cargo_target(policy, floors=floors)
    return shared_cargo_target()


def cargo_child_env(env, *, pin_configured=False, target=None):
    result = dict(_cargo_cache_env(env), CARGO_TARGET_DIR=str(target or shared_cargo_target()))
    if target is not None:
        result['CARGO_BUILD_BUILD_DIR'] = str(target)
    if pin_configured:
        for name in ('RUSTC_WRAPPER', 'SCCACHE_CACHE_SIZE', 'SCCACHE_DIR', 'CARGO_HOME',
                     'PIP_CACHE_DIR', 'npm_config_cache', 'UV_CACHE_DIR', 'TMPDIR'):
            if value := os.environ.get(name):
                result[name] = value
    return result


def _cargo_cache_env(env):
    global _sccache_missing_warned
    result = dict(env, CARGO_INCREMENTAL="0")
    home = Path.home()
    search = env.get('PATH', os.defpath)
    inherited = env.get('RUSTC_WRAPPER')
    named = env.get('PLANGRAPH_SCCACHE') or os.environ.get('PLANGRAPH_SCCACHE')
    inherited_sccache = inherited and Path(inherited).name.lower() in ('sccache', 'sccache.exe')
    if named:
        wrapper = shutil.which(named, path=search)
    else:
        wrapper = ((shutil.which(inherited, path=search) if inherited_sccache else None)
                   or shutil.which(str(home / '.local/bin/sccache'))
                   or shutil.which('sccache', path=search))
    if inherited and not inherited_sccache and (
            not wrapper or Path(inherited).absolute() != Path(wrapper).absolute()):
        result.pop('SCCACHE_DIR', None)
        return result
    if wrapper:
        cap = (env.get('PLANGRAPH_SCCACHE_CACHE_SIZE') or os.environ.get('PLANGRAPH_SCCACHE_CACHE_SIZE')
               or env.get('SCCACHE_CACHE_SIZE') or '20G')
        result.update(RUSTC_WRAPPER=str(Path(wrapper).absolute()),
                      SCCACHE_DIR=str(home / '.cache/plangraph/sccache'),
                      SCCACHE_CACHE_SIZE=cap)
    else:
        result.pop('RUSTC_WRAPPER', None)
        result.pop('SCCACHE_DIR', None)
        if not _sccache_missing_warned:
            _sccache_missing_warned = True
            logging.warning('sccache unavailable; Cargo will build without a compiler cache')
    return result


def disk_free_gb(path):
    path = Path(path).expanduser().absolute()
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / (1024 ** 3)


def worktree_disk_root():
    if not os.environ.get('PLANGRAPH_WORKTREE_MOUNTS'):
        return None
    probe = subprocess.run(['pg-spawnd', 'host', 'work-disk'], capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=30, check=False)
    if probe.returncode:
        reason = probe.stderr.strip()
        raise OSError(reason if reason.startswith('disk_low:') else 'disk_low: work mount probe failed')
    root = Path(json.loads(probe.stdout))
    if not root.is_absolute():
        raise OSError('disk_low: invalid work mount probe response')
    return root


def cargo_target_size_gb(path):
    # Do not follow links into another cache or repository.
    total = 0
    seen = set()
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
                    stat = entry.stat(follow_symlinks=False)
                    if stat.st_nlink > 1 and stat.st_ino:
                        inode = (stat.st_dev, stat.st_ino)
                        if inode in seen:
                            continue
                        seen.add(inode)
                    total += stat.st_blocks * 512 if hasattr(stat, 'st_blocks') else stat.st_size
                except FileNotFoundError:
                    pass  # Cargo renames and unlinks temporary files during builds.
    return total / (1024 ** 3)


CLEANUP_WAIT_ENV = 'PLANGRAPH_CARGO_CLEANUP_WAIT_S'


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


CLI_TESTS_KEEP_ENV = 'PLANGRAPH_CLI_TESTS_KEEP'
CLI_TESTS_KEEP = 3
CLI_TESTS_MAX_AGE_S = 24 * 3600


def _touched(path):
    """Newest mtime of a directory, its entries and their entries (cargo touches the inner ones)."""
    newest = path.stat().st_mtime
    for child in path.iterdir():
        newest = max(newest, child.lstat().st_mtime)
        if child.is_dir() and not child.is_symlink():
            for inner in child.iterdir():
                newest = max(newest, inner.lstat().st_mtime)
    return newest


def prune_builds(target, subdir, keep, max_age=CLI_TESTS_MAX_AGE_S, now=None):
    """Keep at most ``keep`` ``<target>/<subdir>/<hash>`` builds, none untouched for ``max_age`` seconds.

    Call it only under the exclusive cargo lease. Returns the removed directory names."""
    now = time.time() if now is None else now
    root, builds = Path(target) / subdir, []
    if root.is_symlink():
        raise ValueError(f'{root} is a symbolic link; build root not pruned')
    try:
        for entry in root.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                try:
                    builds.append((_touched(entry), entry))
                except OSError:
                    continue  # a build removed meanwhile
    except FileNotFoundError:
        return []
    builds.sort(key=lambda b: b[0], reverse=True)
    removed = []
    for index, (touched, entry) in enumerate(builds):
        if index >= max(keep, 0) or now - touched > max_age:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def prune_cli_tests(target, keep=None, max_age=CLI_TESTS_MAX_AGE_S, now=None):
    """Per-checkout ``cli-tests/<hash>`` builds: the newest ``keep`` (PLANGRAPH_CLI_TESTS_KEEP, default 3)."""
    if keep is None:
        try:
            keep = int(os.environ.get(CLI_TESTS_KEEP_ENV, CLI_TESTS_KEEP))
        except ValueError:
            keep = CLI_TESTS_KEEP
    return prune_builds(target, CLI_TESTS_ROOT, keep, max_age, now)


# Cargo's own output is the evidence that a directory is a cargo target: a CACHEDIR.TAG with
# the signature Cargo writes, or a .rustc_info.json holding a rustc fingerprint. File names alone
# prove nothing, and neither does a bare debug/.
CACHEDIR_SIGNATURE = b'Signature: 8a477f597d28d172789f06886806bc55'
CLI_TESTS_ROOT, POLICY_BIN_ROOT = 'cli-tests', 'policy-bin'
BUILD_ROOTS = (CLI_TESTS_ROOT, POLICY_BIN_ROOT)


def cargo_output(path):
    """True when ``path`` holds a regular CACHEDIR.TAG with Cargo's signature or a real .rustc_info.json."""
    tag, info = Path(path) / 'CACHEDIR.TAG', Path(path) / '.rustc_info.json'
    try:
        if tag.is_file() and not tag.is_symlink() and tag.read_bytes().startswith(CACHEDIR_SIGNATURE):
            return True
        if info.is_file() and not info.is_symlink() and info.stat().st_size <= 1 << 20:
            data = json.loads(info.read_text())
            return isinstance(data, dict) and 'rustc_fingerprint' in data
    except (OSError, ValueError, TypeError):
        pass
    return False


def unsafe_cache_dir(target, checkout):
    """Refuse a cache that is, holds or sits inside a checkout, the home directory or an ancestor of it."""
    checkout, cache, home = Path(checkout).resolve(), Path(target).resolve(), Path.home().resolve()
    if (Path(target).is_symlink() or checkout.is_relative_to(cache) or cache.is_relative_to(checkout)
            or home.is_relative_to(cache) or cache == Path(cache.anchor)):
        raise ValueError('unsafe cache directory')


def identified_cargo_target(target):
    """An empty directory, or one with Cargo's own output at its top or in a build root's builds; never a Git checkout."""
    target = Path(target)
    names = {entry.name for entry in target.iterdir()}
    if not names:
        return True
    if '.git' in names:
        return False
    if cargo_output(target):
        return True
    for root in BUILD_ROOTS:
        base = target / root
        if root in names and base.is_dir() and not base.is_symlink():
            if any(build.is_dir() and not build.is_symlink() and cargo_output(build)
                   for build in base.iterdir()):
                return True
    return False


def reclaim_cargo_target(target, cap, checkout, wait=None, identified=False):
    """Trim the shared target before a build: prune old cli-tests builds, then drop all of it if still over ``cap`` GB.

    Holds the exclusive lease (live builds hold shared ones) and raises OSError when it cannot get it,
    ValueError for a target that is unsafe next to ``checkout`` or, with ``identified``, holds no cargo markers."""
    unsafe_cache_dir(target, checkout)
    if identified and Path(target).exists() and not identified_cargo_target(target):
        raise ValueError(f'{target} is not an identified cargo target; not touched')
    if wait is None:
        try:
            wait = float(os.environ.get(CLEANUP_WAIT_ENV, '30'))
        except ValueError:
            wait = 30.0
        wait = wait if math.isfinite(wait) and wait >= 0 else 30.0
    with cargo_lease(exclusive=True, wait=wait):
        for root in BUILD_ROOTS:   # refuse before any build root is pruned
            if (Path(target) / root).is_symlink():
                raise ValueError(f'{Path(target) / root} is a symbolic link; build root not pruned')
        prune_cli_tests(target)
        prune_builds(target, POLICY_BIN_ROOT, CLI_TESTS_KEEP)
        if target.exists() and cargo_target_size_gb(target) >= cap:
            shutil.rmtree(target)


def _cap_gb(raw):
    if raw is None:
        return None
    raw = raw.strip()
    if not raw.isascii() or '_' in raw:
        return None
    try:
        cap = float(raw)
    except ValueError:
        return None
    return cap if math.isfinite(cap) and cap >= 0 else None


def cargo_target_cap_gb():
    """Use the first valid cap from the environment, host config file, or default."""
    cap = _cap_gb(os.environ.get('PLANGRAPH_CARGO_TARGET_CAP_GB'))
    if cap is not None:
        return cap
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    try:
        cap = _cap_gb((config / 'plangraph' / 'cargo-target-cap-gb').read_text(encoding='utf-8'))
    except (OSError, UnicodeError):
        cap = None
    return DEFAULT_CARGO_TARGET_CAP_GB if cap is None else cap


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


def _safe_cache_dir(target, worktree):
    try:
        unsafe_cache_dir(target, worktree)
    except ValueError:
        return False
    return True


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


def select_work_drive(drives, cleanup):
    """The one drive decision (see host_ready_source.select_drive), measured with ``disk_free_gb``."""
    return select_drive(drives, cleanup, disk_free_gb)


#: Roles that build nothing, so the shared build cache's size cap does not bind them.
NO_BUILD_ROLES = ('review',)


def local_disk_admission(worktree, service, role='implement', movable=False, *, native=False):
    """Return an alternate checkout to launch in, or the disk refusal reason."""
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
        configured_target = _target_for(policy)
        target = configured_target.resolve() if native else configured_target
        unsafe_cache_dir(target, worktree)
        building = role not in NO_BUILD_ROLES
        size = cargo_target_size_gb(target) if building else 0
        if building and cap and size >= cap:
            runs = service.ps()['runs']
            if native:
                runs = [row for row in runs if not row.get('overflow_remote') and row.get('role') == 'implement']
            active = sum(cargo_run_busy(row) for row in runs)
            limit = cap * (1 + active) if native else cap
            if size >= limit:
                measured = f'cargo_target_cap: measured {size:.2f} GB in {target}, cap {limit:g} GB'
                if active:
                    return None, measured + '; busy'
                if configured_target.is_symlink():
                    return None, measured + '; cleanup unavailable for a symlinked target'
                try:
                    reclaim_cargo_target(target, cap, worktree)
                except OSError:
                    return None, measured + '; busy or cleanup unavailable'
        if building:
            # The legacy caches and the pool slots stay under the same cap.
            for extra in _legacy_cache_roots() + _pool_slot_roots(target):
                if extra != target and _safe_cache_dir(extra, worktree) and cargo_target_size_gb(extra) >= cap:
                    _clean_single_cache(extra, service, cap=cap)
        if cap == 0 and building:
            return None, f'cargo_target_cap: cap 0 GB for {target}; no build capacity'
        configured = _configured_work_drives(policy)
        place = movable and configured
        drives = (work_drive_checkouts(configured) if place else
                  [(worktree, checkout_floor(worktree, configured, floor))])
        if not drives:
            return None, 'disk_low: no configured work drive is a launchable Git checkout'

        def check(checkout):
            unsafe_cache_dir(target, checkout)
            actual = policy if Path(checkout) == Path(worktree) else _policy_data(checkout) or {}
            if root := worktree_disk_root():
                actual = dict(actual, worktree_root=root)
            return admission_shortfall(target, getattr(service, 'state', None), actual, floor, disk_free_gb)

        selected, low = select_drive(drives, lambda: _cleanup_before_disk_refusal(worktree, service),
                                     disk_free_gb, check=check)
        if low is not None:
            free, need = low
            reason = f'disk_low: free {free:.2f} GB, floor {need:g} GB'
            _record_disk_floor_alarm(service, free, need, reason)
            return None, reason
        return selected if place else None, None
    except SettingsError as exc:
        raise as_spawn_refused(exc) from None
    except OSError as error:
        if str(error).startswith('disk_low:'):
            return None, str(error)
        return None, 'disk_low: measurement or configuration unavailable'
    except (ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return None, 'disk_low: measurement or configuration unavailable'


def local_disk_refusal(worktree, service, role='implement', movable=False, *, native=False):
    """Return only the disk refusal for callers that do not launch a run."""
    return local_disk_admission(worktree, service, role, movable, native=native)[1]


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
                reason = await asyncio.to_thread(running_disk_refusal, job.cwd or runner.default_cwd, job.role)
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


def running_disk_refusal(worktree, role='implement'):
    """Rule 7.1: stop below 2 GiB; cap the shared cache throughout a run of a building role."""
    try:
        from plangraph.settings import _policy_data
        policy = _policy_data(worktree) or {}
        target = _target_for(policy, floors=False)  # the stop floor below is the runtime limit
        paths = [worktree, target]
        if policy.get('worktree_root'):
            paths.append(policy['worktree_root'])
        free = min(disk_free_gb(path) for path in paths)
        if free < 2:
            return f'disk_low: free {free:.2f} GB, stop 2 GB'
        cap = cargo_target_cap_gb()
        building = role not in NO_BUILD_ROLES
        size = cargo_target_size_gb(target) if building else 0
        if building and size >= cap:
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


def check_host(name, config, state, runner=subprocess.run, timeout=HOST_TIMEOUT):
    return probe_host(name, config, state, runner, timeout, persist=atomic_write_private)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--test-tools', metavar='REPO')
    parser.add_argument('--unit')
    parser.add_argument('--cargo-run', nargs=argparse.REMAINDER, metavar='CARGO_ARG')
    parser.add_argument('--cargo-cache-env', type=Path, metavar='GITHUB_ENV')
    args = parser.parse_args(argv)
    if args.cargo_cache_env is not None:
        env = _cargo_cache_env(os.environ)
        keys = ('RUSTC_WRAPPER', 'SCCACHE_DIR', 'SCCACHE_CACHE_SIZE', 'CARGO_INCREMENTAL')
        lines = {key: env.get(key, '') for key in keys}
        if any('\n' in value or '\r' in value for value in lines.values()):
            parser.error('Cargo cache environment contains a line break')
        with args.cargo_cache_env.open('a', encoding='utf-8') as output:
            output.writelines(f'{key}={value}\n' for key, value in lines.items())
        return 0
    if args.cargo_run is not None:
        if not args.cargo_run:
            parser.error('--cargo-run requires Cargo arguments')
        return subprocess.call(['cargo', *args.cargo_run], env=_cargo_cache_env(os.environ))
    if args.test_tools:
        print(json.dumps(pre_push_report(args.test_tools, args.unit)))
        return 0
    from .overflow import host_config, placement_config, readiness_dir, registry_caps
    from plangraph.fleet_sync.genconfig import work_mounts
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
                work_mounts=work_mounts(local),
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
