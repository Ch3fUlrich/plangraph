"""Bounded, injectable host inspection; unavailable data is never zero drift."""
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .release import git


def _local_git(repo, *args, check=True):
    # Hooks inherit Git's index/repository bindings. Each explicit checkout owns
    # its identity, including when called from a linked worktree.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(repo), *args], check=check,
                          capture_output=True, text=True, timeout=10, env=env)


def checkout_findings(repo, *, default_branch=None):
    """Read production checkout health without fetching or changing it."""
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
        else:
            merged = _local_git(repo, 'merge-base', '--is-ancestor', 'HEAD',
                                'refs/remotes/origin/' + default, check=False)
            if merged.returncode == 1:
                findings.append('unmerged commits on the default branch')
            elif merged.returncode:
                raise ValueError('origin default unavailable')
        flags = _local_git(repo, 'ls-files', '-v', '-z').stdout.split('\0')
        if any(row and (row[0] == 'S' or row[0].islower()) for row in flags):
            findings.append('hidden tracked files: skip-worktree or assume-unchanged')
        if _local_git(repo, 'status', '--porcelain', '--untracked-files=no').stdout:
            findings.append('dirty tracked files')
    except (OSError, ValueError, subprocess.SubprocessError):
        findings.append('checkout or default branch unavailable')
    return findings


def alert_sender(command=None, runner=None):
    """Use live_follow's configured keeper-alert command and final-argument contract."""
    from plangraph.live_follow import Runner

    argv = shlex.split(command if command is not None else os.environ.get('PLANGRAPH_ALERT_CMD', ''))
    if not argv or not shutil.which(argv[0]):
        raise ValueError('PLANGRAPH_ALERT_CMD must name an available keeper alert handler')
    runner = runner or Runner()
    def send(line):
        rc, _ = runner.run([*argv, line])
        if rc:
            raise RuntimeError('keeper alert handler failed')
    return send


def check_checkout(repo, send, *, state_dir, default_branch=None, now=None):
    """Notify on bad/clear transitions, with at most one successful reminder/hour."""
    import fcntl

    from plangraph.routing_support.private_io import atomic_write_private

    now = time.time() if now is None else now
    ident = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:16]
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / (ident + '.json')
    fd = os.open(state / (ident + '.lock'), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'wb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        repair = False
        try:
            old = json.loads(path.read_text())
            if (not isinstance(old, dict) or type(old.get('bad')) is not bool
                    or type(old.get('sent')) not in (int, float) or not math.isfinite(old['sent'])):
                raise ValueError('invalid production checkout alarm state')
        except (OSError, UnicodeError, ValueError, OverflowError, RecursionError):
            old, repair = {'bad': False, 'sent': 0}, True
        findings = checkout_findings(repo, default_branch=default_branch)
        bad = bool(findings)
        if bad != old['bad'] or (bad and now - old['sent'] >= 3600):
            kind = 'alarm' if bad and not old['bad'] else 'reminder' if bad else 'clear'
            line = f'production-checkout {kind}: ' + ('; '.join(findings) if bad else 'checkout healthy')
            # Reserve the dedupe state before delivery: a full disk must not
            # produce an acknowledged alarm on every tick. Failed sends undo
            # the reservation; a crash may defer a retry until the reminder.
            atomic_write_private(path, json.dumps({'bad': bad, 'sent': now}) + '\n')
            try:
                send(line)
            except Exception:
                atomic_write_private(path, json.dumps(old) + '\n')
                raise
            repair = False
        if repair:
            atomic_write_private(path, json.dumps(old) + '\n')
        return findings


def _audit_stub(python, script, state, hook):
    guard = ('[ "${1-}" = committed ] || exit 0' if hook == 'reference-transaction'
             else '[ "${3-}" = 1 ] || exit 0')
    selection = ('''old= new=
while read -r before after ref; do
    case "$before$after" in *[!0-9a-f]*) continue ;; esac
    case "${#before}:${#after}" in 40:40|64:64) ;; *) continue ;; esac
    case "$ref" in
        HEAD|"$headref") old=$before; new=$after ;;
    esac
done
[ -n "$new" ] || exit 0''' if hook == 'reference-transaction' else
                 'old=$1; new=$2')
    # The marker is per Git worktree registration, shared by its two observers.
    # Shell builtins handle ignored phases and duplicate observations: no Python.
    dedupe = ('''printf '%s %s %s\\n' "$old" "$new" "$branch" > "$marker" || exit 0'''
              if hook == 'reference-transaction' else '''if [ -f "$marker" ]; then
    read -r before after previous_branch < "$marker" || true
    [ "$after $previous_branch" != "$new $branch" ] || exit 0
fi
printf '%s %s %s\\n' "$old" "$new" "$branch" > "$marker" || exit 0''')
    # Never wait for timeout: even SIGKILL cannot reap uninterruptible I/O.
    return ('#!/bin/sh\n# plangraph HEAD audit v2: observations must never block Git.\n'
            + guard + '\n' + 'state=' + shlex.quote(str(state)) + '\n' + '''
# Git runs non-bare hooks in the worktree root, including linked worktrees.
gitdir=${GIT_DIR:-.git}
if [ -f "$gitdir" ]; then
    IFS= read -r location < "$gitdir" || exit 0
    gitdir=${location#gitdir: }
fi
headref=
IFS= read -r headref < "$gitdir/HEAD" || exit 0
case "$headref" in
    'ref: refs/heads/'*) headref=${headref#ref: }; branch=${headref#refs/heads/} ;;
    *) branch=detached ;;
esac
key=main
[ ! -f "$gitdir/commondir" ] || key=wt-${gitdir##*/}
marker="$state/last-$key"
''' + selection + '\n' + dedupe + '\n'
            + 'command -v timeout >/dev/null 2>&1 || exit 0\n'
            + 'command -v setsid >/dev/null 2>&1 || exit 0\n'
            + shlex.join(
                ['setsid', 'timeout', '-s', 'KILL', '1', python, '-I', '-S', str(script), str(state)])
            + ' "$PWD" "$old" "$new" "$branch" "$gitdir" </dev/null >/dev/null 2>&1 &\nexit 0\n')


def install_audit(repo, state_dir, *, apply=False):
    """Copy a standalone runtime to state; dry-run unless explicitly applied."""
    from . import head_audit

    repo, state_dir = Path(repo).resolve(), Path(state_dir).resolve()
    script = state_dir / 'head-audit.py'
    hooks = {}
    for name in ('post-checkout', 'reference-transaction'):
        path = Path(_local_git(repo, 'rev-parse', '--path-format=absolute', '--git-path',
                               'hooks/' + name).stdout.strip())
        content = _audit_stub(sys.executable, script, state_dir, name)
        old_command = [sys.executable, '-m', 'plangraph.fleet_sync', 'head-move',
                       '--repo', str(repo), '--state-dir', str(state_dir), '--hook', name, '--']
        legacy = '#!/bin/sh\nexec ' + shlex.join(old_command) + ' "$@"\n'
        foreground = content.replace('command -v setsid >/dev/null 2>&1 || exit 0\n', '').replace(
            'setsid timeout ', 'timeout ', 1).replace(' &\nexit 0\n', ' || true\nexit 0\n')
        previous = foreground.replace('command -v timeout >/dev/null 2>&1 || exit 0\n', '').replace(
            'timeout -s KILL 1 ', '', 1)
        if path.exists() and path.read_text() not in (content, foreground, previous, legacy):
            raise ValueError('existing hook; integrate it explicitly before installing audit')
        hooks[path] = content
    if apply:
        from plangraph.routing_support.private_io import atomic_write_private
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_private(script, Path(head_audit.__file__).read_text())
        for path, content in hooks.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_private(path, content)
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
