"""Shared-config protection and per-leg writer identity.

The config file under ``--git-common-dir`` is shared by every worktree and
every sibling lane.  The guard therefore never treats a key it cannot
attribute as a mutation: only keys this run can be shown to have added (they
appear in the spawn-time baseline diff) *and* that are dangerous count.
Danger is a denylist (command execution, credential redirection, config
injection); every other key a sibling lane or the user writes is recorded at
most and never removed or rolled back.  Offending keys are removed with
``git config --file <path> --unset-all`` and pre-existing values restored
per key - git's own ``config.lock`` read-modify-write.  The file is never
rewritten from a snapshot.  Errors listing the config only record a warning
event: the run is neither failed nor restored.
"""
from pathlib import Path
from contextlib import contextmanager
import os
import re
import subprocess
import tempfile
import time
import uuid

from plangraph.routing_errors import SpawnRefused


class RunWorktreeDependencyNotMerged(SpawnRefused):
    """A declared finished dependency is absent from the fetched main history."""

    code = reason = 'run_worktree_dependency_not_merged'


@contextmanager
def run_worktree_lock(root):
    """Host-wide creation lock; WORKTREE-REAPER's reserved name is exactly .lock."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (13, 36):
                        raise
                    time.sleep(0.05)  # LK_LOCK retries for only ten seconds.
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def create(root, repo, lane_branch, continuation=False, *, finished_dependencies=()):
    """Create one attempt without resetting a branch or removing an earlier worktree.

    Continuations are sequential handoffs: --force permits the existing branch
    to remain attached in the retained, no-longer-running attempt directory.
    """
    root = Path(root).expanduser().resolve()
    def git(*args, check=True):
        return subprocess.run(['git', '-C', str(repo), *args], check=check,
                              capture_output=True, text=True, timeout=120)
    with run_worktree_lock(root):
        git('check-ref-format', 'refs/heads/' + lane_branch)
        if lane_branch.startswith('-'):
            raise SpawnRefused('run_worktree_invalid_branch')
        git('fetch', 'origin', '+refs/heads/main:refs/remotes/origin/main')
        main = git('rev-parse', '--verify', 'refs/remotes/origin/main^{commit}').stdout.strip()
        if not continuation:
            for sha in finished_dependencies:
                if (not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40,64}', sha)
                        or git('merge-base', '--is-ancestor', sha, main, check=False).returncode):
                    raise RunWorktreeDependencyNotMerged('run_worktree_dependency_not_merged')
        base = main
        if continuation:
            base = git('rev-parse', '--verify', 'refs/heads/' + lane_branch + '^{commit}').stdout.strip()
        path = root / ('attempt-' + uuid.uuid4().hex)
        if continuation:
            git('worktree', 'add', '--force', str(path), lane_branch)
        else:
            git('worktree', 'add', '-b', lane_branch, str(path), base)
        return dict(path=str(path), branch=lane_branch, base_sha=base)


async def prepare_worktree(service, record, card, enabled, *, continuation=False):
    """Persist creation evidence before launch; off/dry-run callers do no Git I/O."""
    if not enabled:
        return {}
    import asyncio
    from plangraph.settings import _policy_data
    repo = getattr(service.runner, 'default_cwd', None) or os.getcwd()
    config = _policy_data(repo) or {}
    root = config.get('worktree_root') or service.state / 'worktrees'
    previous = record.get('run_worktree')
    branch = previous['branch'] if previous else card.get('lane_branch') or 'lane/' + record['run_id']
    continuation = continuation or bool(previous) or bool(card.get('continuation'))
    metadata = await asyncio.to_thread(create, root, repo, branch, continuation,
                                      finished_dependencies=card.get('finished_dependencies', ()))
    record.update(run_worktree=metadata, cwd=metadata['path'])
    record.setdefault('worktree_attempts', []).append(metadata)
    service._save(record)
    return dict(cwd=metadata['path'])

# Denylist of dangerous keys (git prints section and key lower-cased, so the
# pattern is matched against the lower-cased key; the original spelling is
# kept for --unset-all since subsections are case-sensitive).
DANGEROUS = re.compile(
    r'core\.(?:hookspath|sshcommand|askpass|fsmonitor|editor|pager|gitproxy|alternaterefscommand)'
    r'|sequence\.editor|diff\.external'
    r'|pager\..*|alias\..*|ssh\.variant'
    r'|(?:diff|merge|filter|mergetool|difftool|remote)\..+\.(?:command|textconv|driver|cmd|clean|smudge|process|uploadpack|receivepack|proxy)'
    r'|uploadpack\.packobjectshook|http\.proxy'
    r'|gpg(?:\..+)?\.program'
    r'|credential\..*'
    r'|url\..+\.(?:push)?insteadof'
    r'|include(?:if)?\..*')


def _list(path):
    result = subprocess.run(['git', 'config', '--file', str(path), '--no-includes',
                             '--list', '-z'], capture_output=True, check=True)
    values = {}
    for item in result.stdout.decode('utf-8', errors='surrogateescape').split('\0'):
        if item:
            key, _, value = item.partition('\n')
            values.setdefault(key, []).append(value)
    return values


def init_guard(work_dir):
    if not work_dir:
        return None
    try:
        result = subprocess.run(['git', '-C', str(work_dir), 'rev-parse', '--git-common-dir'],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        path = (Path(work_dir) / result.stdout.strip()).resolve() / 'config'
        baseline = None
        # Close before git opens it, including on Windows.
        with tempfile.TemporaryDirectory(prefix='plangraph-git-guard-') as directory:
            private = Path(directory) / 'config'
            private.write_bytes(path.read_bytes())
            private.chmod(0o600)
            try:
                baseline = _list(private)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return path, baseline
    except (OSError, subprocess.SubprocessError):
        return None


def check_guard(guard, job):
    if not guard or job is None:
        return
    path, baseline = guard
    if baseline is None:
        job.events.append({'event': 'git_config_warning', 'reason': 'baseline_unavailable'})
        return
    try:
        current = _list(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        job.events.append({'event': 'git_config_warning', 'reason': 'config_list_failed'})
        return
    changed = sorted(k for k in baseline.keys() | current.keys()
                     if sorted(baseline.get(k, [])) != sorted(current.get(k, [])))
    # Mutation = a dangerous key that gained values during this run's window.
    offending = [k for k in changed if DANGEROUS.fullmatch(k.lower())
                 and set(current.get(k, [])) - set(baseline.get(k, []))]
    for key in offending:
        try:
            result = subprocess.run(['git', 'config', '--file', str(path), '--unset-all', key],
                                    capture_output=True)
            if result.returncode not in (0, 5):
                raise ValueError('unset failed')
            for value in baseline.get(key, []):
                subprocess.run(['git', 'config', '--file', str(path), '--add', key, value],
                               capture_output=True, check=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            job.events.append({'event': 'git_config_warning',
                               'reason': 'config_unset_failed', 'keys': [key]})
    if offending:
        job.events.append({'event': 'git_config_mutated', 'keys': sorted(offending)})
    benign = [k for k in changed if k not in set(offending)]
    if benign:
        job.events.append({'event': 'git_config_changed', 'keys': benign})
    if offending:
        job.status = 'failed'
        job.exit_code = 1


def writer_launch(prompt, role, model, run_id):
    if role not in ('implement', 'fix'):
        return prompt, {}
    name = model + ' (plangraph writer)'
    email = run_id + '@writer.plangraph.invalid'
    return (prompt + '\nEnd every commit message with the trailer line: Writer: '
            + model + ' ' + run_id,
            {'GIT_AUTHOR_NAME': name,
             'GIT_AUTHOR_EMAIL': email,
             'GIT_COMMITTER_NAME': name,
             'GIT_COMMITTER_EMAIL': email})
