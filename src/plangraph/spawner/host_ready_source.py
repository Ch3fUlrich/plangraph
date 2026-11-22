"""Probe the source paths actually configured for spawner and remote ps.

Also holds the host-readiness disk helpers (cargo cache roots, work drives, floor alarm)."""
import json
import math
import os
from pathlib import Path
import shlex
import time

from .private_io import atomic_write_private
from .remote_ps import configured_hosts


def check_sources(name, config, command, fail):
    sources = [(config.get('ssh'), config['spawner_src'], False if config.get('registry') else True)]
    ok = True
    matched = bool(config.get('local'))
    seen_hosts = []
    try:
        for host in configured_hosts():
            if host['name'] != name:
                continue
            matched = True
            if host in seen_hosts:
                continue
            seen_hosts.append(host)
            if (host.get('invalid') or 'win_src' in host
                    or host.get('wsl_distro') != config.get('wsl_distro')):
                fail('src_config', 'unsupported_or_invalid')
                ok = False
                continue
            sources.append((host['target'], host.get('src'), False))
    except (OSError, ValueError, KeyError, TypeError):
        fail('src_config', 'unreadable')
        ok = False
    else:
        if not matched:
            fail('src_config', 'missing_ps_host')
            ok = False
    seen = []  # Invalid paths may be unhashable; deduplicate before validating.
    for target, source, checkout in sources:
        key = (target, source, checkout)
        if key in seen:
            continue
        seen.append(key)
        if not isinstance(source, str) or not source or (not config.get('local') and not isinstance(target, str)):
            fail('src_config', 'route_or_path_unknown')
            ok = False
            continue
        if target != config.get('ssh'):
            fail('src_config', f"ps target={target!r} and overflow ssh={config['ssh']!r} "
                 'must be the same string')
            ok = False
            continue
        # Expand only the conventional home prefix, never shell syntax from config.
        path = ('"$HOME"/' + shlex.quote(source[2:]) if source.startswith('~/')
                else shlex.quote(source))
        import_path = path + '/src' if checkout else path
        script = (f'if [ ! -d {path} ]; then echo src_missing; '
                  f'elif (cd / && PYTHONPATH={import_path} python3 -c '
                  + shlex.quote('import plangraph') +
                  ') >/dev/null 2>&1; then echo ok; else echo src_import; fi')
        steps = [{'argv': ['test', '-d', source], 'fail_code': 10},
                 {'argv': ['python3', '-c', 'import plangraph'], 'cwd': '/',
                  'env': {'PYTHONPATH': source + '/src' if checkout else source}, 'fail_code': 11},
                 {'argv': ['echo', 'ok']}]
        result = command('src', script, steps, outputs={10: 'src_missing', 11: 'src_import'})
        if result is None:
            ok = False
        elif result.strip() != 'ok':
            label = result.strip() if result.strip() in ('src_missing', 'src_import') else 'src'
            fail(label, 'failed')
            ok = False
    return ok


def windows_cargo_lease(path, exclusive):
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


def legacy_cache_roots():
    """Per-user caches of earlier layouts, held under the same cap as the shared target."""
    return [(Path.home() / rel).expanduser().absolute()
            for rel in ('.cache/pg-target-po', '.cache/pg-target-warm')]


def pool_slot_roots(primary):
    """Pool slots beside the primary target (`<primary>-pool/slot-N`), as created by cargo_pool."""
    pool = primary.with_name(primary.name + '-pool')
    try:
        return sorted(p for p in pool.glob('slot-*')
                      if p.is_dir() and not p.is_symlink() and '.' not in p.name)
    except OSError:
        return []


def record_disk_floor_alarm(service, free_gb, floor_gb, reason):
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


def configured_work_drives(policy):
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


def select_drive(drives, cleanup, free_gb, *, check=None):
    """Pick the first drive meeting its floor and any additional filesystem checks.

    When none qualifies, clean up once and retry. Return the selected path or the
    first shortfall as a (free GiB, floor GiB) pair.
    """
    if not drives:
        raise ValueError('select_work_drive select_drive needs at least one drive')

    def pick():
        first_low = None
        for path, floor in drives:
            free = free_gb(path)
            low = (free, floor) if free < floor else check(path) if check else None
            if low is None:
                return path, None
            if first_low is None:
                first_low = low
        return None, first_low

    selected, low = pick()
    if low is not None:
        cleanup()
        return pick()
    return selected, None


def work_drive_checkouts(drives):
    """Resolve configured drive paths that contain a launchable Git checkout."""
    from .prompt_input import resolve_cwd
    from plangraph.routing_errors import ParameterRefused

    checkouts = []
    for path, floor in drives:
        try:
            checkout = resolve_cwd(str(Path(path).expanduser()), None)
        except ParameterRefused:
            continue
        checkouts.append((checkout, floor))
    return checkouts


def admission_shortfall(target, state, policy, floor, free_gb):
    """Probe the Cargo target, state, and both supervisor and run worktree roots."""
    paths = [target]
    if state is not None:
        paths.extend((state, state / 'trees', policy.get('worktree_root') or state / 'worktrees'))
    for path in paths:
        free = free_gb(path)
        if free < floor:
            return free, floor
    return None


def checkout_floor(worktree, drives, default):
    """The configured floor of the work drive holding ``worktree`` (same filesystem), else ``default``."""
    def device(path):
        path = Path(path)
        while not path.exists() and path != path.parent:
            path = path.parent
        return path.stat().st_dev
    try:
        mine = device(worktree)
    except OSError:
        return default
    for path, floor in drives or ():
        try:
            if device(path) == mine:
                return floor
        except OSError:
            continue
    return default
