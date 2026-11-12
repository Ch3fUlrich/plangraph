"""Pinned canary-first rollout with injectable transport and health commands."""
import base64
import inspect
import json
import os
import re
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from .release import release_path


@contextmanager
def rollout_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('advance already running; inspect stale lock before removing it') from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


def worker(config):
    import os
    import re
    import shutil
    import subprocess
    from pathlib import Path

    repo, sha = config['repo'], config['sha']
    root = Path(repo['live_dir']).parent
    if not Path(repo['worktrees_root']).is_absolute():
        raise ValueError('worktrees_root must be absolute')
    if '--dry-run' not in repo['health_checks']['spawn']:
        raise ValueError('spawn health command requires --dry-run')
    cleanup = Path(repo['worktrees_root']).resolve()
    if root.resolve().is_relative_to(cleanup) or cleanup.is_relative_to(root.resolve()):
        raise ValueError('live root overlaps cleanup')
    releases, current = root / 'releases', root / 'current'
    windows = config['windows']
    pointer = root / 'current.pointer' if windows else current
    lock = root / '.advance.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    previous = None
    target, events, step = releases / sha, [], 'prepare'

    def switch(value):
        temporary = root / '.current.next'
        temporary.unlink(missing_ok=True)
        if value is None:
            pointer.unlink(missing_ok=True)
            return
        if windows:
            temporary.write_text(value)
        else:
            temporary.symlink_to(value, target_is_directory=True)
        os.replace(temporary, pointer)

    def health(directory, post=False):
        for name in (('doctor', 'whoami', 'spawn') if post else ('doctor', 'whoami')):
            subprocess.run(repo['health_checks'][name], cwd=directory, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)

    try:
        previous = (pointer.read_text() if windows else os.readlink(pointer)) if pointer.exists() else None
        if releases.is_symlink() or target.is_symlink():
            raise ValueError('release paths must not be symlinks')
        releases.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            staging = releases / ('.' + sha)
            if staging.exists():
                shutil.rmtree(staging)
            subprocess.run(['git', 'clone', '--no-hardlinks', '--no-checkout', repo['root'],
                            str(staging)], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
            subprocess.run(['git', '-C', str(staging), 'checkout', '--detach', sha],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            os.replace(staging, target)
        actual = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'],
                                         text=True, stderr=subprocess.DEVNULL, timeout=30).strip()
        if actual != sha:
            raise ValueError('pinned checkout mismatch')
        events.append({'step': step, 'status': 'OK'})
        step = 'pre-health'
        health(target)
        events.append({'step': step, 'status': 'OK'})
        step = 'switch'
        switch(str(target))
        events.append({'step': step, 'status': 'OK'})
        step = 'post-health'
        health(target if windows else current, post=True)
        events.append({'step': step, 'status': 'OK'})
        step = 'prune'
        pinned = {target.resolve(), (root / previous).resolve() if previous else target.resolve()}
        candidates = sorted((p for p in releases.iterdir() if re.fullmatch('[0-9a-f]{40,64}', p.name)
                             and p.is_dir() and not p.is_symlink()),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        keep = set(pinned)
        for path in candidates:
            if len(keep) < 3:
                keep.add(path.resolve())
        for path in candidates:
            if path.resolve() not in keep:
                shutil.rmtree(path)
        events.append({'step': step, 'status': 'OK'})
        return {'ok': True, 'events': events}
    except (OSError, ValueError, subprocess.SubprocessError):
        events.append({'step': step, 'status': 'FAILED'})
        try:
            switch(previous)
            events.append({'step': 'rollback', 'status': 'OK'})
        except OSError:
            events.append({'step': 'rollback', 'status': 'FAILED'})
        return {'ok': False, 'events': events}
    finally:
        lock.unlink()


def transport(host, config):
    if host['transport'] == 'local':
        return worker(config)
    code = inspect.getsource(worker) + '\nimport json; print(json.dumps(worker(json.loads(' + repr(json.dumps(config)) + '))))'
    encoded = base64.b64encode(code.encode()).decode('ascii')
    command = [config['repo'].get('python') or 'python3', '-c',
               f"import base64;exec(base64.b64decode('{encoded}'))"]
    remote = subprocess.list2cmdline(command) if host['os'] == 'windows' else shlex.join(command)
    return json.loads(subprocess.check_output(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '--',
         host.get('ssh_target') or host['id'], remote],
        text=True, stderr=subprocess.DEVNULL, timeout=900))


def advance(registry, sha, hosts, go, dry_run=False, record=None, log=None, run=transport):
    if dry_run:
        return _advance(registry, sha, hosts, go, True, record, log, run)
    with rollout_lock(Path.home() / '.local/state/plangraph/fleet-sync.lock'):
        return _advance(registry, sha, hosts, go, False, record, log, run)


def _advance(registry, sha, hosts, go, dry_run, record, log, run):
    if not go or not re.fullmatch(r'D-[0-9]+', go):
        raise ValueError('router GO required (D-825)')
    if not re.fullmatch('[0-9a-f]{40,64}', sha) or json.loads(
            (record or release_path()).read_text())['sha'] != sha:
        raise ValueError('SHA must match recorded release')
    selected = {h['id']: h for h in registry['hosts']}
    if not hosts or len(set(hosts)) != len(hosts):
        raise ValueError('explicit canary-first host order required')
    configs = []
    problems = []
    for name in hosts:
        if name not in selected:
            problems.append(f'{name}: unknown host')
            continue
        host = selected[name]
        repo = host.get('repos', {}).get('plangraph')
        if not repo or not repo.get('live_dir'):
            problems.append(f'ps-only host {name}: no live dir')
            continue
        if host.get('transport') not in ('local', 'ssh'):
            problems.append(f'{name}: invalid transport {host.get("transport")!r}')
            continue
        if not repo.get('worktrees_root'):
            problems.append(f'{name}: missing worktrees_root')
            continue
        if not isinstance(repo.get('health_checks'), dict):
            problems.append(f'{name}: missing health_checks')
            continue
        path = PureWindowsPath if host['os'] == 'windows' else PurePosixPath
        live, cleanup = path(repo['live_dir']), path(repo['worktrees_root'])
        if host['transport'] == 'local' and host['os'] != 'windows':
            live = Path(live).parent.resolve() / live.name
            cleanup = Path(cleanup).resolve()
        if (live.name != 'current'
                or not path(repo['worktrees_root']).is_absolute()
                 or not path(repo['live_dir']).is_absolute() or '..' in live.parts
                 or '..' in cleanup.parts
                or live.parent.is_relative_to(cleanup) or cleanup.is_relative_to(live.parent)):
            problems.append(f'{name}: live root must be outside worktree cleanup')
            continue
        for key in ('doctor', 'whoami', 'spawn'):
            command = repo['health_checks'].get(key)
            if not isinstance(command, list) or not command or not all(
                    isinstance(arg, str) and arg for arg in command):
                problems.append(f'{name}: health commands must be argv arrays ({key})')
        if any(problem.startswith(f'{name}:') for problem in problems):
            continue
        if '--dry-run' not in repo['health_checks']['spawn']:
            problems.append(f'{name}: spawn health command requires --dry-run')
            continue
        configs.append((host, {'repo': repo, 'sha': sha, 'windows': host['os'] == 'windows'}))
    if problems:
        raise ValueError('\n'.join(problems))
    for host, config in configs:
        if dry_run:
            print(f"{host['id']}: prepare {sha}; pre-health; switch current; post-health; prune")
            continue
        try:
            result = run(host, config)
        except (OSError, ValueError, subprocess.SubprocessError):
            result = {'ok': False, 'events': [{'step': 'transport', 'status': 'FAILED'}]}
        logfile = log or Path.home() / '.local/state/plangraph/fleet-sync.log'
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as stream:
            for event in result['events']:
                stream.write(json.dumps(dict(host=host['id'], sha=sha, go=go, **event)) + '\n')
        print(f"{host['id']}: {'OK' if result['ok'] else 'FAILED'}")
        if not result['ok']:
            return 1
    return 0
