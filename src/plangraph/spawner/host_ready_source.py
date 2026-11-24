"""Probe the source paths actually configured for spawner and remote ps.

Also holds the host-readiness disk helpers (cargo cache roots, work drives, floor alarm)."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from .doctor import clean
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


def configured_build_target(policy, free_gb, *, floors=True):
    """The Cargo target on the `build_drives` entry with the most free space above its floor.

    None when the policy lists no build drive. Refuses with `disk_low` when every
    listed drive is under its floor, unreadable or not mounted. A drive entry with a
    `mount` must have that mount present. `floors=False` is the running guard: the
    admission floor does not apply once a build runs, only the runtime stop floor.
    """
    drives = policy.get('build_drives')
    if not drives:
        return None
    best, shortfalls = None, []
    for entry in drives:
        floor = float(entry.get('disk_floor_gb', 5))
        mount = entry.get('mount')
        if not Path(entry['path']).is_dir() or (
                mount and not (os.path.ismount(mount) and Path(entry['path']).is_relative_to(mount))):
            # An unmounted path would be measured through its parent's filesystem.
            shortfalls.append(f"{entry['path']}: not mounted")
            continue
        try:
            free = free_gb(entry['path'])
        except OSError as error:
            shortfalls.append(f"{entry['path']}: {error}")
            continue
        if free >= floor or not floors:
            if best is None or free > best[0]:
                best = (free, entry['path'])
        else:
            shortfalls.append(f"{entry['path']}: {free:.2f} GB < floor {floor:g} GB")
    if best is None:
        raise OSError('disk_low: no build drive meets its floor (' + '; '.join(shortfalls) + ')')
    return Path(best[1]).expanduser().absolute() / 'cargo-target'


def select_drive(drives, cleanup, free_gb, *, check=None):
    """Pick the drive with the most free space among those meeting their floor and any extra checks.

    When none qualifies, clean up once and retry. Return the selected path or the
    first shortfall as a (free GiB, floor GiB) pair.
    """
    if not drives:
        raise ValueError('select_work_drive select_drive needs at least one drive')

    def pick():
        best, first_low, first_error = None, None, None
        for path, floor in drives:
            try:
                free = free_gb(path)
            except OSError as error:
                # An unreadable drive is ineligible; the others may still qualify.
                first_error = first_error or error
                continue
            try:
                low = (free, floor) if free < floor else check(path) if check else None
            except OSError as error:
                first_error = first_error or error
                continue
            if low is None:
                if best is None or free > best[0]:
                    best = (free, path)
            elif first_low is None:
                first_low = low
        if best is not None:
            return best[1], None
        if first_low is None and first_error is not None:
            raise first_error
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
    """Prefer the exact checkout's floor, then the first drive on its filesystem."""
    def device(path):
        path = Path(path)
        while not path.exists() and path != path.parent:
            path = path.parent
        return path.stat().st_dev
    try:
        checkout = Path(worktree).expanduser().resolve()
        mine = device(checkout)
    except OSError:
        return default
    device_floor = None
    for path, floor in drives or ():
        try:
            path = Path(path).expanduser().resolve()
            if path == checkout:
                return floor
            if device_floor is None and device(path) == mine:
                device_floor = floor
        except OSError:
            continue
    return default if device_floor is None else device_floor


HOST_TIMEOUT = 120


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


def probe_host(name, config, state, runner, timeout, *, persist):
    record = dict(host=name, ok=False, checked_at=None, failures=[], reachable=False,
                  mem_available_mb=None, disk_free_gb=None, checkout_sha=None,
                  main_sha=None, doctor_ok=None, keeper_ok=None,
                  spawner_session=None, live_runs=None, behind_main=None,
                  src_ok=None, trust_ok=None, tools_ok=None, python_ok=None, tokens_ok=None,
                  sandbox_ok=None, sandbox_error=None)
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
                readiness = measure_argv(
                    'trust', ['pg-host-readiness', '--project', config['repo'], '--json'], json.loads)
                if isinstance(readiness, dict):
                    record['sandbox_ok'] = readiness.get('sandbox_ok') is True
                    record['sandbox_error'] = clean(readiness.get('sandbox_error') or '') or None
                    record['trust_ok'] = readiness.get('errors') == []
                    if not record['trust_ok']:
                        fail('trust', 'readiness check failed')
                else:
                    record['trust_ok'] = False
                    if readiness is not None:
                        fail('trust', 'invalid_response')
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
            for mount in config.get('work_mounts', []):
                if not isinstance(mount, str) or not mount.startswith('/') or mount == '/':
                    fail('disk', 'invalid work mount')
                    continue
                if command_argv('disk.mount', 'mountpoint', '-q', '--', mount) is None:
                    fail('disk', f'work mount unavailable: {mount}')
                    continue
                free = measure_argv('disk', ['df', '-Pk', '--', mount],
                    lambda s: int(s.splitlines()[-1].split()[3]) // (1024 * 1024))
                if free is not None:
                    record.setdefault('work_mounts', {})[mount] = free
                    if free < config.get('disk_floor_gb', 5):
                        fail('disk', f'disk_low: {mount}: free {free} GB')
            if config.get('work_drives'):
                def free_gb(s):
                    return int(s.splitlines()[-1].split()[3]) // (1024 * 1024)
                record['work_drives'] = [
                    dict(path=d['path'], disk_floor_gb=d.get('disk_floor_gb', config.get('disk_floor_gb', 5)),
                         disk_free_gb=measure_argv('disk', ['df', '-Pk', '--', d['path']], free_gb))
                    for d in config['work_drives']]
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
            drives = record.get('work_drives')
            if drives:
                # A host with work drives is ready while one drive meets its own floor.
                if not any(d['disk_free_gb'] is not None and d['disk_free_gb'] >= d['disk_floor_gb'] for d in drives):
                    fail('disk', f"disk_low: free {drives[0]['disk_free_gb']} GB")
            elif record['disk_free_gb'] is not None and record['disk_free_gb'] < config.get('disk_floor_gb', 5):
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
        persist(Path(state) / (name + '.json'), json.dumps(record))
    except (MemoryError, RecursionError):
        raise
    except Exception:
        # Persistence errors also leave a reportable failure, never abort the fan-out.
        fail('state', 'write_failed')
        record['ok'] = False
    return record
