"""Pinned canary-first rollout with injectable transport and health commands."""
import inspect
import json
import os
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

from .release import release_path


def worker(config):
    import os
    import re
    import shutil
    import subprocess
    from pathlib import Path

    repo, sha = config['repo'], config['sha']
    root = Path(repo['live_dir']).parent
    cleanup = Path(repo['worktrees_root']).resolve()
    if root.resolve().is_relative_to(cleanup) or cleanup.is_relative_to(root.resolve()):
        raise ValueError('live root overlaps cleanup')
    releases, current = root / 'releases', root / 'current'
    windows = config['windows']
    pointer = root / 'current.pointer' if windows else current
    previous = (pointer.read_text() if windows else os.readlink(pointer)) if pointer.exists() else None
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

    def health(directory):
        for name in ('doctor', 'whoami', 'spawn'):
            subprocess.run(repo['health_checks'][name], cwd=directory, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)

    try:
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
        health(target if windows else current)
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


def transport(host, config):
    if host['transport'] == 'local':
        return worker(config)
    code = inspect.getsource(worker) + '\nimport json; print(json.dumps(worker(json.loads(' + repr(json.dumps(config)) + '))))'
    command = [config['repo'].get('python') or 'python3', '-c', code]
    remote = subprocess.list2cmdline(command) if host['os'] == 'windows' else shlex.join(command)
    return json.loads(subprocess.check_output(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '--', host['id'], remote],
        text=True, stderr=subprocess.DEVNULL, timeout=900))


def advance(registry, sha, hosts, go, dry_run=False, record=None, log=None, run=transport):
    if not go or not re.fullmatch(r'D-[0-9]+', go):
        raise ValueError('router GO required (D-825)')
    if not re.fullmatch('[0-9a-f]{40,64}', sha) or json.loads(
            (record or release_path()).read_text())['sha'] != sha:
        raise ValueError('SHA must match recorded release')
    selected = {h['id']: h for h in registry['hosts']}
    if not hosts or len(set(hosts)) != len(hosts):
        raise ValueError('explicit canary-first host order required')
    configs = []
    for name in hosts:
        host = selected[name]
        repo = host['repos']['plangraph']
        path = PureWindowsPath if host['os'] == 'windows' else PurePosixPath
        live, cleanup = path(repo['live_dir']), path(repo['worktrees_root'])
        if (host['transport'] not in ('local', 'ssh') or live.name != 'current'
                or not live.is_absolute() or '..' in live.parts
                or live.parent.is_relative_to(cleanup) or cleanup.is_relative_to(live.parent)):
            raise ValueError('live root must be outside worktree cleanup')
        for key in ('doctor', 'whoami', 'spawn'):
            command = repo['health_checks'][key]
            if not isinstance(command, list) or not command or not all(
                    isinstance(arg, str) and arg for arg in command):
                raise ValueError('health commands must be argv arrays')
        configs.append((host, {'repo': repo, 'sha': sha, 'windows': host['os'] == 'windows'}))
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
