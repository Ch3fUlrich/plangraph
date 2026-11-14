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
from .prompt_input import checkout_env


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


@contextmanager
def run_tree(state, repo=None):
    """Select fetched main without moving live trees; lease until context exit."""
    from contextlib import ExitStack
    from .runner import is_terminal
    import json
    state = Path(state).resolve()
    repo = Path(repo or os.environ.get('PLANGRAPH_SPAWNER_REPO') or
                Path(__file__).resolve().parents[3])
    root = state / 'trees'
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *map(str, args)], check=True,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=10 if args[0] == 'fetch' else 30).stdout.strip()
    def valid(path):
        return (not path.is_symlink() and path.is_dir() and
                re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', path.name))
    stale = None
    # Fetches serialize independently of creation; neither waits for live runs.
    with run_worktree_lock(root / '.fetch'):
        try:
            git('fetch', '--no-write-fetch-head', 'origin',
                '+refs/heads/main:refs/remotes/origin/main')
        except (OSError, subprocess.SubprocessError) as exc:
            stale = 'fetch_timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'fetch_failed'
        # Even offline, prefer the verified tracking ref, never directory mtimes.
        sha = git('rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
        raise RuntimeError('run_tree_invalid_sha')
    path = root / sha
    with ExitStack() as resources:
        # Slow creation at A cannot monopolize selection/creation at B.
        with run_worktree_lock(root / '.locks' / sha):
            if path.is_symlink() or (path.exists() and not valid(path)):
                raise RuntimeError('run_tree_invalid')
            if not path.exists():
                # Reuse only this missing registration; never prune other trees.
                git('worktree', 'add', '--detach', '--force', '--force', path, sha)
            if not (path / '.git').is_file():
                raise RuntimeError('run_tree_invalid')
            if git('-C', path, 'rev-parse', '--verify', 'HEAD^{commit}') != sha:
                raise RuntimeError('run_tree_invalid_head')
            os.utime(path, None)  # Grace for callers that persist after lease release.
            if os.name != 'nt':
                import fcntl
                lease = resources.enter_context((root / (sha + '.lease')).open('a+b'))
                fcntl.flock(lease, fcntl.LOCK_SH)
        # Unknown identities and unreadable records withhold cleanup, not admission.
        live = set()
        try:
            for record in (state / 'runs').glob('*/record.json'):
                row = json.loads(record.read_text(encoding='utf-8'))
                if row.get('survivors') or not is_terminal(row.get('status')):
                    identity = row.get('tree_sha')
                    if not isinstance(identity, str) or not re.fullmatch(
                            r'(?:[0-9a-f]{40}|[0-9a-f]{64})', identity):
                        live = None
                        break
                    live.add(identity)
        except (OSError, ValueError, TypeError, AttributeError):
            live = None
        # Windows has no shared lease here: selection works, cleanup stays off.
        for old in root.iterdir() if live is not None and os.name != 'nt' else ():
            try:
                if old == path or old.name in live or not valid(old):
                    continue
                # Never wait behind another SHA's slow creation or active cleanup.
                import fcntl
                with (root / '.locks' / old.name / '.lock').open('a+b') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    _cleanup_tree(old, root, git)
            except (OSError, subprocess.SubprocessError):
                continue  # A dirty/unreadable candidate must not starve the others.
        yield dict(tree_sha=sha, tree_path=str(path), **({'tree_stale': stale} if stale else {}))


def _cleanup_tree(old, root, git):
    """Remove only expired, clean trees whose POSIX lease is exclusively available."""
    import fcntl
    if time.time() - old.stat().st_mtime <= 7 * 86400:
        return
    lease_path = root / (old.name + '.lease')
    with lease_path.open('a+b') as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        git('worktree', 'remove', old)  # Never force-remove a dirty tree.
    lease_path.unlink(missing_ok=True)


def create(root, repo, lane_branch, continuation=False, *, finished_dependencies=()):
    """Create one attempt without resetting a branch or removing an earlier worktree.

    Continuations are sequential handoffs: --force permits the existing branch
    to remain attached in the retained, no-longer-running attempt directory.
    """
    root = Path(root).expanduser().resolve()
    def git(*args, check=True, failure='run_worktree_create_failed'):
        try:
            return subprocess.run(['git', '-C', str(repo), *args], check=check,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=checkout_env())
        except subprocess.TimeoutExpired:
            raise SpawnRefused('run_worktree_fetch_timeout' if args[0] == 'fetch'
                               else 'run_worktree_git_timeout') from None
        except (OSError, subprocess.CalledProcessError):
            raise SpawnRefused(failure) from None
    if not isinstance(lane_branch, str) or lane_branch.startswith('-'):
        raise SpawnRefused('run_worktree_invalid_branch')
    git('check-ref-format', 'refs/heads/' + lane_branch, failure='run_worktree_invalid_branch')
    if not continuation and any(not isinstance(sha, str) or not re.fullmatch(
            r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', sha) for sha in finished_dependencies):
        raise RunWorktreeDependencyNotMerged('run_worktree_dependency_not_merged')
    # Network latency must not monopolize the host's creation/reaper lock.
    if continuation:
        git('rev-parse', '--verify', 'refs/heads/' + lane_branch + '^{commit}',
            failure='run_worktree_branch_missing')
    from .base_fresh import fresh_worktree_base
    main = fresh_worktree_base(repo, base='refs/heads/' + lane_branch if continuation else 'origin/main')
    with run_worktree_lock(root):
        if not continuation and not git('show-ref', '--verify', '--quiet', 'refs/heads/' + lane_branch,
                                        check=False).returncode:
            raise SpawnRefused('run_worktree_branch_exists')
        if not continuation:
            for sha in finished_dependencies:
                if git('merge-base', '--is-ancestor', sha, main, check=False).returncode:
                    raise RunWorktreeDependencyNotMerged('run_worktree_dependency_not_merged')
        base = main
        if continuation:
            base = git('rev-parse', '--verify', 'refs/heads/' + lane_branch + '^{commit}',
                       failure='run_worktree_branch_missing').stdout.strip()
        path = root / ('attempt-' + uuid.uuid4().hex)
        if continuation:
            git('worktree', 'add', '--force', str(path), lane_branch)
        else:
            git('worktree', 'add', '-b', lane_branch, str(path), base)
        return dict(path=str(path), branch=lane_branch, base_sha=base)


def selected_checkout(service, record):
    """Recover the pre-worktree default for legacy attempt records, even if pruned."""
    cwd = record.get('cwd')
    attempt = (record.get('run_worktree') or {}).get('path')
    if cwd and attempt and Path(cwd).resolve() == Path(attempt).resolve():
        cwd = None
    return record.setdefault('selected_cwd', str(
        cwd or getattr(service.runner, 'default_cwd', None) or os.getcwd()))


async def prepare_worktree(service, record, card, enabled, *, continuation=False, repo=None):
    """Persist creation evidence before launch; off/dry-run callers do no Git I/O."""
    if not enabled:
        return {}
    import asyncio
    from plangraph.settings import _policy_data
    repo = repo or selected_checkout(service, record)
    record.setdefault('selected_cwd', str(repo))
    config = _policy_data(repo) or {}
    root = config.get('worktree_root') or service.state / 'worktrees'
    previous = record.get('run_worktree')
    branch = previous['branch'] if previous else card.get('lane_branch') or 'lane/' + record['run_id']
    continuation = continuation or bool(previous) or bool(card.get('continuation'))
    metadata = await asyncio.to_thread(create, root, repo, branch, continuation,
                                      finished_dependencies=card.get('finished_dependencies', ()))
    metadata.update(created_by_run_id=record['run_id'], continuation=continuation)
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
                             '--list', '-z'], capture_output=True, check=True, env=checkout_env())
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
                                capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, env=checkout_env())
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
                                    capture_output=True, env=checkout_env())
            if result.returncode not in (0, 5):
                raise ValueError('unset failed')
            for value in baseline.get(key, []):
                subprocess.run(['git', 'config', '--file', str(path), '--add', key, value],
                               capture_output=True, check=True, env=checkout_env())
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
