"""RAM-based local admission and opt-in public-repository SSH placement."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from typing import NamedTuple

from plangraph.keeper.credential_guard import mask
from plangraph.fleet_sync.genconfig import config_dir, generate
from plangraph.routing_errors import ParameterRefused

from .private_io import atomic_write_private


def __getattr__(name):
    # Keep the compatibility attribute without loading the executable module on import.
    if name == 'cli':
        from importlib import import_module
        return import_module('.cli', __package__)
    raise AttributeError(name)


class Decision(NamedTuple):
    host: str | None
    reason: str


class RemoteRefusal(ValueError):
    """Safe, value-free remote failure reason."""


def failure_reason(exc):
    """Describe the failure without exposing exception values or private paths."""
    if isinstance(exc, OSError):
        return 'remote spawn OS operation failed: ' + (os.strerror(exc.errno) if exc.errno else 'I/O error')
    if isinstance(exc, KeyError):
        return 'remote spawn configuration missing a required field'
    if isinstance(exc, TypeError):
        return 'remote spawn input has an invalid type'
    return 'remote spawn input or receipt has an invalid value'


def registry_caps():
    """Read the same registry location and JSON format as fleet-sync."""
    try:
        registry = json.loads((config_dir() / 'hosts.json').read_text())
        if not isinstance(registry['hosts'], list):
            raise ValueError('hosts must be a list')
        return registry
    except (OSError, ValueError, KeyError, TypeError):
        logging.warning('hosts registry missing or malformed; overflow disabled, local RAM defaults 3000/2048 MB')
        return {'hosts': [], 'self': None}


def placement_config(registry, role=None):
    """Derive placement routes from the authoritative registry."""
    try:
        config = generate(registry)['overflow-hosts.json']
        config['registry'] = True
        if role is not None:
            config['hosts'] = {name: host for name, host in config['hosts'].items()
                               if role in host.get('roles', [role])}
        return config
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logging.warning('invalid hosts registry routes; overflow disabled')
        return {'hosts': {}, 'public_repos': []}


def host_config(registry, name):
    for host in registry.get('hosts', []):
        if not isinstance(host, dict):
            continue
        aliases = host.get('aliases')
        names = [host.get('id'), *(aliases if isinstance(aliases, list) else [])]
        if not isinstance(name, str) or not any(
                isinstance(alias, str) and alias.lower() == name.lower() for alias in names):
            continue
        return host
    return {}


def placement_policy(registry, config):
    name = os.environ.get('PLANGRAPH_HOST_NAME') or registry.get('self')
    host = host_config(registry, name)
    name = host.get('id') or name
    hosts = config.get('hosts', {})
    local = hosts.get(name, {}) if isinstance(hosts, dict) else {}
    return name, (host.get('orchestration_only') is True or
                  isinstance(local, dict) and local.get('orchestration_only') is True)


def host_caps(registry, name, role='implement'):
    host = host_config(registry, name)
    if host:
        caps = host.get('caps')
        warmup = host.get('warmup')
        ramp = warmup.get('ramp_mb') if isinstance(warmup, dict) else None
        if (isinstance(caps, dict) and type(caps.get('mem_floor_mb')) is int
                and caps['mem_floor_mb'] >= 0 and isinstance(ramp, dict) and ramp
                and all(type(value) is int and value > 0 for value in ramp.values())):
            return ramp.get(role, max(ramp.values())), caps['mem_floor_mb']
    logging.warning('host %r: missing or malformed registry caps; not an overflow target', name)
    return None


def local_limits(registry, role='implement'):
    caps = host_caps(registry, os.environ.get('PLANGRAPH_HOST_NAME') or registry.get('self'), role)
    if caps is None:
        logging.warning('local admission using conservative RAM defaults: floor 3000 MB, role 2048 MB')
    return caps or (2048, 3000)


def memory_refusal(free_mb, reservation_mb, floor_mb):
    """Worker-core mem_fit: effective_free_mb - reservation_mb >= mem_floor_mb.

    These callers supply MemAvailable without unmaterialized slot reservations.
    The independent OOM guard remains in force.
    """
    if free_mb - reservation_mb >= floor_mb:
        return None
    return f'memory: free {free_mb} MB, floor {floor_mb} MB, role {reservation_mb} MB'


def memory_floor_record(memory, role_mb, floor, detail, role='implement'):
    return dict(run_id='run-' + uuid.uuid4().hex, status='refused', role=role,
                reason='host_memory_floor', overflow_reason=detail, exit_code=2,
                mem_available_mb=memory, floor_mb=floor, role_mb=role_mb, ended=time.time(),
                error=f'host_memory_floor: MemAvailable {memory} MB, floor {floor} MB, role {role_mb} MB; {detail}')


def _disk_refusal(row):
    drives = row.get('work_drives')
    if drives is not None:
        if not isinstance(drives, list) or not drives:
            return 'malformed readiness'
        low = []
        for d in drives:
            if not isinstance(d, dict):
                return 'malformed readiness'
            free, floor = d.get('disk_free_gb'), d.get('disk_floor_gb', row.get('disk_floor_gb', 5))
            if any(type(v) not in (int, float) or not 0 <= v < float('inf') for v in (free, floor)):
                return 'malformed readiness'
            if free >= floor:
                return None
            low.append(free)
        return f'disk_low: free {low[0]} GB'
    if row.get('disk_free_gb') is not None and any(
            type(value) not in (int, float) or not 0 <= value < float('inf')
            for value in (row['disk_free_gb'], row.get('disk_floor_gb', 5))):
        return 'malformed readiness'
    if row.get('disk_free_gb') is not None and row['disk_free_gb'] < row.get('disk_floor_gb', 5):
        return f"disk_low: free {row['disk_free_gb']} GB"
    return None


def decide(local_mem_mb, role_mb, floor_mb, ready_records, public, *,
            remote_first=False, local_refusal=None, host_order=(),
            compare_local=False, local_fallback=False):
    """Select from config-enriched readiness records; age is computed on read."""
    local_reason = local_refusal or memory_refusal(local_mem_mb, role_mb, floor_mb)
    if not local_reason and not remote_first and not compare_local:
        return Decision(None, 'local room')
    if not public:
        if not local_reason or remote_first:
            return Decision(None, local_reason or 'local_fallback_no_remote')
        return Decision(None, local_reason + '; queue: repo is not public (absent or unknown allowlist)')
    candidates, refused = [], []
    records = ready_records.items() if remote_first else sorted(ready_records.items())
    for index, (name, row) in enumerate(records):
        reason = row.get('refusal')
        if not reason:
            if not row.get('ssh'):
                reason = 'no_route'
            elif row.get('ok') is not True:
                reason = 'failed readiness'
            elif row.get('fresh') is not True:
                reason = 'stale readiness'
            elif (disk_reason := _disk_refusal(row)):
                reason = disk_reason
            elif any(type(row.get(key)) is not int or row[key] < 0
                     for key in ('live_runs', 'mem_available_mb')):
                reason = 'malformed readiness'
            elif any(type(row.get(key)) is not int or row[key] < minimum
                     for key, minimum in (('role_mb', 1), ('floor_mb', 0))):
                reason = 'missing or malformed registry caps'
            else:
                reason = memory_refusal(row['mem_available_mb'], row['role_mb'], row['floor_mb'])
        if reason:
            refused.append(f'{name}: {reason}')
        else:
            if remote_first:
                index = host_order.index(name) if name in host_order else len(host_order)
            candidates.append((row.get('placement_fallback') is True,
                               -(row['mem_available_mb'] - row['floor_mb']), index, name))
    if compare_local and not local_reason:
        candidates.append((local_fallback, -(local_mem_mb - floor_mb), -1, None))
    if candidates:
        fallback, neg_headroom, _, name = min(candidates)
        if compare_local:
            return Decision(name, f"{'fallback: ' if fallback else ''}{-neg_headroom} MB above memory floor")
        return Decision(name, 'remote_most_free_ram' if remote_first else local_reason)
    if remote_first:
        return Decision(None, local_reason or 'local_fallback_no_remote')
    return Decision(None, local_reason + '; queue: ' + ('; '.join(refused) or 'no overflow hosts configured'))


def readiness_dir():
    return Path.home() / '.local/state/plangraph/host-ready'


def read_readiness(name):
    from .host_ready import fresh
    if Path(name).name != name or name in ('.', '..'):
        raise ValueError('invalid host name')
    data = json.loads((readiness_dir() / (name + '.json')).read_text())
    if data['host'] != name:
        raise ValueError('host mismatch')
    return {**data, 'fresh': fresh(dict(data, ok=True))}


def worker_host_refusal(name, state=None):
    """Consume the phase published by Rust at the placement origin."""
    if not name:
        return None
    state = Path(state or os.environ.get('PLANGRAPH_SPAWN_STATE') or
                 Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'plangraph')
    try:
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('invalid host name')
        phase = json.loads((state / 'worker-hosts' / (name + '.json')).read_text())['phase']
        if phase == 'ready':
            return None
        if phase in ('unreachable', 'rejoining'):
            return 'worker host ' + phase
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 'worker host state unavailable'


def apply_worker_refusals(records, state):
    for name, row in records.items():
        if refusal := worker_host_refusal(name, state):
            row['refusal'] = refusal
    return records


SANDBOX_UNAVAILABLE = 'sandbox_unavailable:'


def sandbox_refusal(row):
    if row.get('sandbox_ok') is True:
        return None
    error = row.get('sandbox_error')
    if isinstance(error, str) and error.startswith(SANDBOX_UNAVAILABLE):
        return error
    detail = error if isinstance(error, str) and error else 'workspace-write sandbox has no successful probe'
    return f'{SANDBOX_UNAVAILABLE} {detail}; operator action: review the bwrap AppArmor profile or user namespace sysctl'


REMOTE_PLACEMENT_CLOSED = 'remote placement closed'


def ready_records(config, role='implement'):
    records = {}
    registry = registry_caps()
    for name, host in config.get('hosts', {}).items():
        row = {}
        try:
            row = dict(host)
            data = read_readiness(name)
            row.update({key: data.get(key) for key in ('ok', 'live_runs', 'mem_available_mb', 'disk_free_gb', 'fresh',
                                                     'sandbox_ok', 'sandbox_error')})
            if data.get('work_drives') is not None:
                row['work_drives'] = data['work_drives']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            row['refusal'] = 'missing or malformed readiness'
        if config.get('registry') and row.get('remote_placement') is not True:
            row.setdefault('refusal', REMOTE_PLACEMENT_CLOSED)  # the host's switch is off
        caps = host_caps(registry, name, role)
        if caps is None:
            row.setdefault('refusal', 'missing or malformed registry caps')
        else:
            row.update(role_mb=caps[0], floor_mb=caps[1])
        records[name] = row
    return records


def local_resources(state=None):
    from . import cli
    runs = cli.SpawnService(state=state).ps()['runs']
    return sum(row.get('status') == 'running' for row in runs), available_memory_mb()


def _windows_available_memory_mb():
    """MemAvailable's Windows twin: GlobalMemoryStatusEx ullAvailPhys."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                    *((name, ctypes.c_ulonglong) for name in (
                        'ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                        'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual'))]
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError('GlobalMemoryStatusEx failed')
    return status.ullAvailPhys // (1024 * 1024)


def available_memory_mb():
    # Windows has no /proc: every local spawn died with ENOENT before launch (2026-11-09).
    if os.name == 'nt':
        return _windows_available_memory_mb()
    try:
        memory = next(int(line.split()[1]) // 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.startswith('MemAvailable:'))
    except (StopIteration, IndexError) as exc:
        raise ValueError('MemAvailable missing') from exc
    return memory


def swap_used_pct():
    try:
        values = {line.split(':')[0]: int(line.split()[1])
                  for line in Path('/proc/meminfo').read_text().splitlines()
                  if line.startswith(('SwapTotal:', 'SwapFree:'))}
        total, free = values['SwapTotal'], values['SwapFree']
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError('swap readings missing or malformed') from exc
    if not 0 <= free <= total:
        raise ValueError('invalid swap readings')
    return 100 * (total - free) / total if total else 0


def local_health_refusal(config, memory):
    if memory < config.get('local_min_available_mb', 4096):
        return 'local_min_available_mb'
    try:
        swap = swap_used_pct()
    except (OSError, ValueError):
        return 'local_swap_unavailable'
    if swap > config.get('local_max_swap_used_pct', 90):
        return 'local_max_swap_used_pct'
    return None


def repos_public(config, repo, diff=None):
    """Every repository contributing checkout or diff context must be public."""
    if diff is not None and not diff.get('repo'):
        raise ParameterRefused('diff_ref requires repo')
    allowed = {Path(p).expanduser().resolve() for p in config.get('public_repos', [])}
    return all(Path(p).expanduser().resolve() in allowed
               for p in [repo, *([diff.get('repo')] if diff is not None else [])])


def target_checkout(host, card):
    """Map only registered public repository identities to the target checkout."""
    source = (card.get('diff_ref') or {}).get('repo') or card.get('repo')
    other = card.get('repo')
    if source is not None and other is not None and Path(source).resolve() != Path(other).resolve():
        raise RemoteRefusal('checkout and diff name different repositories')
    if source is None or not host.get('registry'):
        return host['repo']
    path = Path(source).expanduser().resolve()
    for name, root in host['public_repo_ids'].items():
        if path == Path(root).expanduser().resolve() and name in host['repos']:
            return host['repos'][name]
    raise RemoteRefusal('public repository checkout unavailable on target')


def stage_review_diff(diff, directory):
    """Pin the exact commits in a disposable clone for review without a push."""
    from .prompt_input import checkout_env
    def git(repo, *args):
        return subprocess.run(['git', '-C', repo, *args], capture_output=True, check=True,
                              text=True, encoding='utf-8', errors='replace',
                              env=checkout_env(), timeout=30).stdout.strip()
    source = diff['repo']
    clone = str(directory / 'review.git')
    git(source, 'clone', '-q', '--shared', '--bare', '--', source, clone)
    for key in ('base', 'head'):
        diff[key] = git(source, 'rev-parse', '--verify', '--end-of-options', diff[key] + '^{commit}')
        git(clone, 'update-ref', 'refs/review/' + key, diff[key])
    boundary = git(source, 'merge-base', diff['head'], 'refs/remotes/origin/main')
    if boundary == diff['head']:
        return []
    bundle = directory / 'review.bundle'
    git(clone, 'bundle', 'create', str(bundle), 'refs/review/base', 'refs/review/head', '^' + boundary)
    if bundle.stat().st_size > 64 * 1024 * 1024:
        raise RemoteRefusal('review bundle exceeds transfer limit')
    return [bundle]


def remote_spawn(name, host, card, prompt, options):
    """Use private staging, quote every remote argument, never echo SSH stderr."""
    options = list(options)
    if 'cwd' in card or any(v == '--cwd' or v.startswith('--cwd=') for v in options):
        raise RemoteRefusal('explicit cwd cannot overflow to another checkout')
    from .remote_ps import invocation
    alias = host['ssh']
    checkout = target_checkout(host, card)
    def ssh(command, *, binary=False, steps=None, plan=None, **kwargs):
        """Run a script on the host's login shell, or a step plan where no shell exists (WSL)."""
        encoding = {} if binary else {'text': True, 'encoding': 'utf-8', 'errors': 'replace'}
        remote, body = invocation(host, command, steps, **(plan or {}))
        if body is not None:
            kwargs['input'] = body if binary else body.decode('utf-8')
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                               '--', alias, remote], capture_output=True,
                              timeout=165, **encoding, **kwargs)
    try:
        staging = ssh('mktemp -d')
    except (OSError, subprocess.SubprocessError):
        raise RemoteRefusal('remote staging failed') from None
    if staging.returncode:
        raise RemoteRefusal('remote staging failed')
    directory = staging.stdout.strip()
    if not directory.startswith('/') or '\n' in directory:
        raise RemoteRefusal('remote staging path invalid')
    q = shlex.quote
    bundle_files = []
    try:
        with tempfile.TemporaryDirectory() as local:
            rewritten = json.loads(json.dumps(card))
            if rewritten.get('run_kind') == 'native-codex':
                rewritten['repo'] = checkout
            diff = rewritten.get('diff_ref')
            if diff:
                from plangraph.planner.seats import _check_diff_ref
                _check_diff_ref(diff)
                if host.get('registry'):
                    try:
                        bundle_files = stage_review_diff(diff, Path(local))
                    except (OSError, subprocess.SubprocessError):
                        raise RemoteRefusal('could not prepare exact review commits') from None
                diff['repo'] = checkout
            atomic_write_private(Path(local) / 'card.json', json.dumps(rewritten))
            atomic_write_private(Path(local) / 'prompt.txt', prompt)
            files = [Path(local) / 'card.json', Path(local) / 'prompt.txt', *bundle_files]
            if host.get('wsl_distro'):
                for path in files:
                    target = directory + '/' + path.name
                    copied = ssh('', binary=True, steps=[{
                        'write_file': {'path': target, 'mode': 0o600},
                        'stdin_hex': path.read_bytes().hex()}])
                    if copied.returncode:
                        raise RemoteRefusal('remote copy failed')
            else:
                copied = subprocess.run(['scp', '--', *map(str, files), alias + ':' + q(directory + '/')],
                                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                                        timeout=60)
                if copied.returncode:
                    raise RemoteRefusal('remote copy failed')
        commands = []
        if host.get('env_file'):
            commands.append('set -a && . ' + q(host['env_file']) + ' && set +a')
        commands.append('cd ' + q(checkout))
        if diff:
            prepare = [f'cd {q(checkout)}']
            prepare_steps = []
            if bundle_files:
                fetch_main = ['git', 'fetch', '--no-write-fetch-head', '-q', 'origin', 'main']
                fetch_bundle = ['git', 'fetch', '--no-write-fetch-head', '--no-tags',
                                '--', directory + '/review.bundle', 'refs/review/head']
                prepare.extend(shlex.join(argv) for argv in (fetch_main, fetch_bundle))
                prepare_steps.extend({'argv': argv} for argv in (fetch_main, fetch_bundle))
            for rev in ('base', 'head'):
                object_ref = diff[rev] + '^{commit}'
                present = shlex.join(['git', 'cat-file', '-e', object_ref])
                fetch_argv = ['git', 'fetch', '--no-write-fetch-head', '-q', 'origin', '--', diff[rev]]
                fetch = shlex.join(fetch_argv)
                prepare.append(f'({present} || ({fetch} && {present}))')
                prepare_steps.append({'ensure_commit': {'sha': diff[rev], 'fetch_argv': fetch_argv}})
            if ssh(' && '.join(prepare), steps=prepare_steps, plan={'cwd': checkout}).returncode:
                raise RemoteRefusal('remote checkout cannot resolve the reviewed commits')
        command = ['python3', '-m', 'plangraph.spawner.cli']
        if host.get('state_dir'):
            command.extend(['--state', host['state_dir']])
        command.extend(['spawn', '--card',
                   '@' + directory + '/card.json', '--prompt-file', directory + '/prompt.txt'])
        spawn = shlex.join(command + options)
        nonce = secrets.token_hex(16)
        begin, end = (f'PLANGRAPH_RECEIPT_{nonce}_{suffix}' for suffix in ('BEGIN', 'END'))
        commands.append("printf '%s\\n' " + q(begin))
        env = dict(PLANGRAPH_HOST_NAME=name, PLANGRAPH_HOST=name,
                   PLANGRAPH_PLACED_HOST=name, PYTHONPATH=host['spawner_src'],
                   PLANGRAPH_PLACEMENT_REASON=host.get('placement_reason') or 'registry placement')
        assignments = ' '.join(f'{key}={q(value)}' for key, value in env.items())
        commands.append('{ ' + assignments + ' ' + spawn
                        + "; rc=$?; printf '\n%s\n' " + q(end) + ' ; exit "$rc"; }')
        spawn_plan = dict(cwd=checkout)
        if host.get('env_file'):
            spawn_plan['env_file'] = host['env_file']
        spawn_steps = [{'argv': command + options, 'env': env, 'receipt': {'begin': begin, 'end': end}}]
        try:
            result = ssh(' && '.join(commands), steps=spawn_steps, plan=spawn_plan)
        except (OSError, subprocess.SubprocessError):
            return {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'unknown',
                    'host': name, 'acknowledged_unknown': True, 'reason': 'remote_launch_uncertain'}
        # Mask before parsing/exposing any remote output, including JSON keys.
        # Never retry an acknowledged spawn merely because its receipt is unreadable.
        output, _ = mask(result.stdout)
        lines = output.splitlines()
        if lines.count(begin) != 1 or lines.count(end) != 1 or lines.index(begin) >= lines.index(end):
            return {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'unknown',
                    'host': name, 'acknowledged_unknown': True,
                    'reason': 'receipt_unbracketed'}
        try:
            receipt = json.loads('\n'.join(lines[lines.index(begin) + 1:lines.index(end)]))
        except ValueError:
            receipt = None
        if isinstance(receipt, dict) and receipt.get('status') == 'refused':
            reason = receipt.get('reason')
            if card.get('run_kind') == 'native-codex' and isinstance(reason, str) and reason.startswith(SANDBOX_UNAVAILABLE):
                raise RemoteRefusal(reason)
            raise RemoteRefusal('target rejected admission')
        if (isinstance(receipt, dict) and isinstance(receipt.get('run_id'), str)
                and receipt['run_id'].strip()):
            return receipt
        return {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'unknown',
                'host': name, 'acknowledged_unknown': True}
    except subprocess.SubprocessError:
        raise RemoteRefusal('remote preparation failed before launch') from None
    finally:
        try:
            staged = [directory + '/' + name for name in ('card.json', 'prompt.txt', 'review.bundle')]
            ssh('rm -f -- ' + ' '.join(q(path) for path in staged) + '; rmdir -- ' + q(directory),
                steps=[{'argv': ['rm', '-f', '--', *staged]}, {'argv': ['rmdir', '--', directory]}],
                plan={'keep_going': True})
        except (OSError, subprocess.SubprocessError):
            logging.warning('remote staging cleanup could not be confirmed')


def native_repo(card):
    repo = card.get('repo')
    if not isinstance(repo, str) or not repo.strip() or not Path(repo).is_absolute():
        raise ValueError('native-codex requires an absolute card.repo')
    return Path(repo).resolve()


async def native_spawn(self, card, options):
    """Placement is already admitted; Rust owns the native writer lifecycle."""
    import asyncio, time
    from plangraph.fleet_snapshot import _projection_binary
    from .private_io import atomic_write_private
    import socket
    if options.get('role', 'implement') != 'implement' or options.get('cwd'):
        raise ValueError('native-codex requires implement role and a host-relative card.repo')
    if options.get('dry_run'):
        raise ValueError('native-codex dry-run is not supported')
    repo = native_repo(card)
    from plangraph.routing_errors import SpawnRefused
    from plangraph.planner.routing import rule_level
    try:
        effort = rule_level(card)
    except (TypeError, ValueError) as exc:
        raise SpawnRefused(str(exc)) from exc
    binary = _projection_binary()
    from .host_ready import harness_path
    tool_env = dict(os.environ)
    tool_env['PATH'] = harness_path(tool_env, native_writer=True)
    # Resolve on the placed host before creating a run or acknowledging admission.
    probe = await asyncio.create_subprocess_exec(binary, 'native-tools', '--check-sandbox',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=tool_env)
    tools, error = await probe.communicate()
    if probe.returncode:
        raise SpawnRefused(error.decode(errors='replace').strip())
    tool_environment = json.loads(tools)
    placement = options.get('_placement')
    host = (placement or {}).get('host') or os.environ.get('PLANGRAPH_HOST_NAME') or socket.gethostname()
    if placement:
        placement = dict(placement, host=host)
    reason = sandbox_refusal(tool_environment)
    if reason:
        return self.save_refusal(SpawnRefused(reason), card=card, placement=placement,
                                 host=host, reason=reason, run_kind='native-codex')
    record = dict(run_id='run-' + uuid.uuid4().hex, run_kind='native-codex',
        host=host, status='running', started=time.time(), role='implement', writer_family='openai',
        base_ref=card.get('base_ref'), repo=str(repo),
        plan=dict(effort=effort, p_source='override'))
    record.update(tool_environment)
    from .exit_outcome import native_harness_fields
    record.update(native_harness_fields(record['host']))
    if placement:
        record['placement'] = placement
    from .item_key import annotate_item
    from .notify import annotate_native
    annotate_item(record, card)
    annotate_native(record, card)
    directory = self._path(record['run_id']).parent
    self._save(record)
    request = dict(record=record, card=card, prompt=options.get('prompt', ''), record_path=str(self._path(record['run_id'])))
    atomic_write_private(directory / 'native-request.json', json.dumps(request))
    from .host_ready import cargo_lease, cargo_child_env
    lease = None
    try:
        lease = await asyncio.to_thread(cargo_lease)
        process = await asyncio.create_subprocess_exec(binary, 'native-writer',
            str(directory / 'native-request.json'), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
            env=cargo_child_env(os.environ))
    except (OSError, RuntimeError):
        if lease: lease.close()
        record.update(status='failed', exit_code=6, reason='native worker launch failed', ended=time.time())
        self._save(record)
        return record
    except BaseException:
        if lease: lease.close()
        raise
    from .proc_kill import root_identity
    identity = root_identity(process.pid)
    async def finish():
        try:
            _, error = await process.communicate()
            import fcntl
            with (directory / 'writer-supervisor.lock').open('ab') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                final = self.result(record['run_id'])
                if final.get('resume_number'):
                    return
                if final.get('status') == 'running':
                    final.update(status='failed', exit_code=process.returncode or 6,
                                 reason=error.decode(errors='replace')[-4096:], ended=time.time())
                final.setdefault('ended', time.time())
                self._save(final)
        except asyncio.CancelledError:
            survivors = await self.runner.kill_recorded(process.pid, identity)
            await process.wait()
            record.update(self.result(record['run_id']))
            record.update(status='failed' if survivors else 'cancelled', exit_code=130, ended=time.time())
            if survivors: record.update(reason='kill_incomplete', survivors=survivors)
            self._save(record)
            raise
        finally:
            lease.close()
    self.tasks[record['run_id']] = asyncio.create_task(finish())
    if options.get('wait_seconds') is None:
        await self.tasks[record['run_id']]
    elif options.get('wait_seconds', 0) > 0:
        await asyncio.wait([self.tasks[record['run_id']]], timeout=options['wait_seconds'])
    return self.result(record['run_id'])


def main(argv=None):
    from . import cli
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('verb', choices=['spawn'])
    parser.add_argument('--repo', required=True)
    parser.add_argument('--card', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--role', default='implement')
    parser.add_argument('--cwd')
    parser.add_argument('--state', help='invoking spawn state directory (local only; defaults to the service state)')
    args, options = parser.parse_known_args(argv)
    local = [*(['--state', args.state] if args.state is not None else []),
             'spawn', '--card', '@' + args.card]
    if args.cwd is not None:
        options.extend(['--cwd', args.cwd])
    if args.role != 'implement':
        options.extend(['--role', args.role])
    try:
        registry = registry_caps()
        config = placement_config(registry, args.role)
        if placement_policy(registry, config)[1] or (config.get('registry') and args.role == 'review'):
            return cli.main([*local, Path(args.prompt).read_text(), *options])
        role_mb, floor_mb = local_limits(registry, args.role)
        _, memory = local_resources(args.state) if args.state is not None else local_resources()
        from .host_ready import local_disk_admission
        try:
            card = json.loads(Path(args.card).read_text())
        except (OSError, ValueError):
            card = None  # Local CLI owns malformed input diagnostics.
        if (card or {}).get('run_kind') == 'native-codex':
            return cli.main([*local, Path(args.prompt).read_text(), *options])
        from .prompt_input import resolve_cwd
        cwd = resolve_cwd(args.cwd, card)
        named = cwd or ((card or {}).get('diff_ref') or {}).get('repo') or (card or {}).get('repo')
        local_host, _ = placement_policy(registry, config)
        disk = worker_host_refusal(local_host, args.state)
        selected = None
        if not disk:
            selected, disk = local_disk_admission(named or args.repo, cli.SpawnService(state=args.state),
                                                  args.role, movable=not named)
        if decide(memory, role_mb, floor_mb, {}, False, local_refusal=disk).reason == 'local room':
            placement = ['--cwd', selected] if selected is not None else []
            return cli.main([*local, Path(args.prompt).read_text(), *options, *placement])
        if card is None:
            card = json.loads(Path(args.card).read_text())
        repo = Path(cwd or args.repo).resolve()
        diff = card.get('diff_ref')
        public = repos_public(config, repo, diff)
        if not cwd and diff and Path(diff.get('repo')).resolve() != repo:
            public = False
        prompt = Path(args.prompt).read_text()
        records = apply_worker_refusals(ready_records(config, args.role), args.state)
        attempts = 0
        while True:
            decision = decide(memory, role_mb, floor_mb, records, public, local_refusal=disk)
            if decision.host is None or attempts == 2:
                detail = decision.reason if decision.host is None else 'refused: retry limit'
                detail = detail.replace('; queue:', '; refused:')
                refusals = [f'{name}: {row["refusal"]}' for name, row in sorted(records.items())
                            if row.get('refusal')]
                reported = detail.replace('; refused: ', '; ').removeprefix('refused: ').split('; ')
                detail = '; '.join([detail, *(reason for reason in refusals if reason not in reported)])
                record = memory_floor_record(memory, role_mb, floor_mb, detail, args.role)
                if not memory_refusal(memory, role_mb, floor_mb):
                    record.update(reason=detail, error=detail)
                cli.SpawnService(state=args.state)._save(record)
                print(json.dumps(record))
                print(record['error'], file=sys.stderr)
                return 2
            name = decision.host
            attempts += 1
            try:
                if refusal := worker_host_refusal(name, args.state):
                    raise RemoteRefusal(refusal)
                result = remote_spawn(name, config['hosts'][name], card, prompt, options)
                refusal = 'remote spawn failed'
            except RemoteRefusal as exc:
                result, refusal = None, str(exc)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result, refusal = None, failure_reason(exc)
            if result is not None:
                result.update(host=name, overflow_reason=decision.reason)
                print(mask(json.dumps(result))[0])
                return 0
            records[name]['refusal'] = refusal
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('overflow input or resource check failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
