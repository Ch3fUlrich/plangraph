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


def select_drive(drives, cleanup, free_gb):
    """The one drive decision: the first configured drive at or above its floor.

    ``drives`` is ``[(path, floor_gb)]``. When none qualifies, ``cleanup`` runs once
    and every drive is measured again. Returns ``(path, None)`` for the selected
    drive, or ``(None, (free_gb, floor_gb))`` of the first drive when all stay low.
    """
    if not drives:
        raise ValueError('select_work_drive select_drive needs at least one drive')

    def pick():
        return next((p for p, floor in drives if free_gb(p) >= floor), None)
    selected = pick()
    if selected is None:
        cleanup()
        selected = pick()
    if selected is not None:
        return selected, None
    first_path, first_floor = drives[0]
    return None, (free_gb(first_path), first_floor)
