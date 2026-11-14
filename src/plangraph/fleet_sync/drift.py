"""Bounded, injectable host inspection; unavailable data is never zero drift."""
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .release import git


def _local_git(repo, *args, check=True):
    # Hooks inherit Git's index/repository bindings. Each explicit checkout owns
    # its identity, including when called from a linked worktree.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(repo), *args], check=check,
                          capture_output=True, text=True, timeout=10, env=env)


def check_checkout(repo, send, *, default_branch=None, routing='routing'):
    """Inspect tracked files only and reuse the fleet-bus alarm sink."""
    findings = []
    try:
        default = default_branch or _local_git(
            repo, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD').stdout.strip().removeprefix('origin/')
        branch = _local_git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', check=False)
        _local_git(repo, 'rev-parse', '--verify', 'HEAD^{commit}')
        if branch.returncode == 1:
            findings.append('detached HEAD')
        elif branch.returncode:
            raise ValueError('branch unavailable')
        elif branch.stdout.strip() != default:
            findings.append(f'wrong branch {branch.stdout.strip()}, want {default}')
        if _local_git(repo, 'status', '--porcelain', '--untracked-files=no').stdout:
            findings.append('dirty tracked files')
    except (OSError, ValueError, subprocess.SubprocessError):
        findings.append('checkout or default branch unavailable')
    if findings:
        send(routing, 'production-checkout alarm: ' + '; '.join(findings))
    return findings


def process_chain():
    """Read command names only, never cmdline, arguments or environment."""
    chain, seen, pid = [], set(), os.getpid()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            stat = (Path('/proc') / str(pid) / 'stat').read_text()
            start, end = stat.index('('), stat.rindex(')')
            ppid = int(stat[end + 2:].split()[1])
            chain.append({'pid': pid, 'ppid': ppid, 'comm': stat[start + 1:end]})
            pid = ppid
        except (OSError, ValueError, IndexError):
            break  # A parent can exit between reads.
    return chain


def audit_head(repo, state_dir, hook, args, transactions=''):
    """Append HEAD moves from post-checkout and committed ref transactions."""
    import fcntl

    branch = _local_git(repo, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
    if hook == 'post-checkout':
        if len(args) != 3 or args[2] != '1':
            return  # File-only checkouts never move HEAD.
        moves = [args[:2]]
    elif hook == 'reference-transaction':
        if args != ['committed']:
            return
        rows = [line.split() for line in transactions.splitlines()]
        heads = [r[:2] for r in rows if len(r) == 3 and r[2] == 'HEAD']
        moves = heads or [r[:2] for r in rows if len(r) == 3 and r[2] == branch]
    else:
        raise ValueError('unknown audit hook')
    path = Path(state_dir) / 'head-moves.jsonl'
    for old, new in moves:
        if not all(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha) for sha in (old, new)):
            raise ValueError('invalid audit sha')
        row = {'time': datetime.now(UTC).isoformat(), 'old_sha': old, 'new_sha': new,
               'branch': branch.removeprefix('refs/heads/') or 'detached',
               'process_chain': process_chain()}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'r+', encoding='utf-8') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            # The two hooks can observe the same checkout. Retain one line.
            last = None
            for line in stream:
                last = line
            previous = json.loads(last) if last else {}
            if all(previous.get(k) == row[k] for k in ('old_sha', 'new_sha', 'branch')):
                continue
            stream.write(json.dumps(row, ensure_ascii=True) + '\n')


def install_audit(repo, state_dir, *, apply=False):
    """Dry-run by default; refuse existing hooks rather than overwrite them."""
    repo, state_dir = Path(repo).resolve(), Path(state_dir).resolve()
    hooks = {}
    for name in ('post-checkout', 'reference-transaction'):
        path = Path(_local_git(repo, 'rev-parse', '--path-format=absolute', '--git-path',
                               'hooks/' + name).stdout.strip())
        command = [sys.executable, '-m', 'plangraph.fleet_sync', 'head-move',
                   '--repo', str(repo), '--state-dir', str(state_dir), '--hook', name, '--']
        content = '#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n'
        if path.exists() and path.read_text() != content:
            raise ValueError('existing hook; integrate it explicitly before installing audit')
        hooks[path] = content
    if apply:
        for path, content in hooks.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o700)
    return [str(path) for path in hooks]


def transport(host):
    live = host['repos']['plangraph']['live_dir']
    command = ['git', '-C', live, 'rev-parse', 'HEAD']
    if host['transport'] == 'ssh':
        remote = (subprocess.list2cmdline(command) if host['os'] == 'windows'
                  else shlex.join(command))
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
                   '--', host.get('ssh_target') or host['id'], remote]
    elif host['transport'] != 'local':
        raise ValueError('unsupported transport')
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()


def rows(registry, release, query=transport, count=None):
    count = count or (lambda live, target: int(git('rev-list', '--count', f'{live}..{target}')))
    result = []
    for host in registry['hosts']:
        live, behind = 'unknown', 'unknown'
        try:
            sha = query(host)
            if not re.fullmatch('[0-9a-f]{40,64}', sha):
                raise ValueError('invalid sha')
            live = sha
            if release != 'unknown':
                behind = count(live, release)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            behind = 'unknown'
        result.append({'host': host['id'], 'live_sha': live, 'release_sha': release, 'behind': behind})
    return result
