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
from contextlib import contextmanager, ExitStack
import os
import re
import subprocess
import tempfile
import time
import uuid

from plangraph.routing_errors import SpawnRefused


@contextmanager
def run_tree(state, repo=None):
    """Pin one immutable code tree; leases bridge selection to durable run records."""
    import json
    if os.name != 'nt':
        import fcntl
    state = Path(state).resolve()
    repo = Path(repo or os.environ.get('PLANGRAPH_SPAWNER_REPO') or
                Path(__file__).resolve().parents[3])
    root = state / 'trees'
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *map(str, args)],
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10 if args[0] == 'fetch' else 30).stdout.strip()
    def valid(path):
        return (not path.is_symlink() and path.is_dir() and
                re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', path.name))
    with ExitStack() as resources:
        with run_worktree_lock(root):
            stale = None
            try:
                git('fetch', '--no-write-fetch-head', 'origin',
                    '+refs/heads/main:refs/remotes/origin/main')
            except (OSError, subprocess.SubprocessError) as exc:
                stale = 'fetch_timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'fetch_failed'
            cached = []
            for candidate in root.iterdir():
                try:
                    if valid(candidate): cached.append((candidate.stat().st_mtime, candidate))
                except OSError:
                    continue
            existing = [p for _, p in sorted(cached, reverse=True)]
            try:
                sha = (existing[0].name if stale and existing else
                       git('rev-parse', '--verify', 'origin/main^{commit}'))
            except (OSError, subprocess.SubprocessError):
                sha = git('rev-parse', '--verify', 'HEAD^{commit}')
                stale = stale or 'main_unavailable'
            path = root / sha
            if path.exists() and not valid(path):
                raise RuntimeError('run_tree_invalid')
            if not path.exists():
                git('worktree', 'add', '--detach', path, sha)
            os.utime(path, None)  # Startup grace also covers spawnd's adapter-to-record handoff.
            if os.name != 'nt':
                lease = resources.enter_context((root / (sha + '.lease')).open('a+b'))
                fcntl.flock(lease, fcntl.LOCK_SH)
            # Unknown/corrupt records withhold cleanup, never admission.
            try:
                live = set()
                for record in (state / 'runs').glob('*/record.json'):
                    row = json.loads(record.read_text(encoding='utf-8'))
                    # Python and Rust status is authoritative even without ended.
                    terminal = ('completed', 'failed', 'cancelled', 'stalled', 'timeout',
                                'timed_out', 'refused', 'limited', 'dry_run', 'needs_input',
                                'orphaned', 'terminated_after_answer')
                    if row.get('survivors') or row.get('status') not in terminal:
                        live.add(row.get('tree_sha'))
            except (OSError, ValueError, TypeError, AttributeError):
                live = None
            # Windows has no shared lease implementation: spawn, but never reap there.
            for old in existing if live is not None and os.name != 'nt' else ():
                try:
                    if old == path or old.name in live:
                        continue
                    _cleanup_tree(old, root, git)
                except (OSError, subprocess.SubprocessError):
                    continue  # One dirty/unreadable tree must not starve later candidates.
        yield dict(tree_sha=sha, tree_path=str(path), **({'tree_stale': stale} if stale else {}))


def _cleanup_tree(old, root, git):
    """Reap one expired POSIX tree only after obtaining its exclusive lease."""
    import fcntl
    if time.time() - old.stat().st_mtime <= 7 * 86400:
        return
    lease_path = root / (old.name + '.lease')
    with lease_path.open('a+b') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        git('worktree', 'remove', old)  # Never force-remove a dirty tree.
    lease_path.unlink(missing_ok=True)


async def fresh_spawn(service, options):
    """A fresh per-run supervisor, not a reload/restart of the owning MCP service."""
    import asyncio
    import json
    import sys
    from plangraph.routing_support import background_process_options
    state = service.state.resolve()
    context = run_tree(state)
    tree = await asyncio.to_thread(context.__enter__)
    child = None
    try:
        env = dict(os.environ, PYTHONPATH=str(Path(tree['tree_path']) / 'src'),
                   PLANGRAPH_RUN_TREE_SHA=tree['tree_sha'], PYTHONDONTWRITEBYTECODE='1')
        child = await asyncio.create_subprocess_exec(sys.executable, '-m',
            'plangraph.spawner.cli', '--supervise', cwd=tree['tree_path'], env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024, **background_process_options(new_session=True, platform=os.name))
        payload = {k: v for k, v in options.items() if k != 'wait_seconds'}
        child.stdin.write((json.dumps(dict(payload, state=str(state), _tree=tree, _admitted=True,
            _cwd=getattr(getattr(service, 'runner', None), 'default_cwd', os.getcwd()))) + '\n').encode())
        await child.stdin.drain()
        child.stdin.close()
        line = await child.stdout.readline()
        if not line:
            raise RuntimeError('run_tree_worker_failed')
        result = json.loads(line)
        if result.get('status') == 'refused':
            raise SpawnRefused(result.get('error') or result.get('reason') or 'spawn refused')
        async def finish():
            try:
                await child.wait()
            finally:
                context.__exit__(None, None, None)
        task = asyncio.create_task(finish())
        for record in result.get('seats') or result.get('writers') or [result]:
            service.tasks[record['run_id']] = task
        if options.get('wait_seconds', 0) is None:
            await task
        elif options.get('wait_seconds', 0) > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), options['wait_seconds'])
            except asyncio.TimeoutError:
                pass
        if 'run_id' in result and hasattr(service, 'result'):
            return service.result(result['run_id'])
        return result
    except BaseException:
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
        context.__exit__(None, None, None)
        raise


def worker_environment(service):
    """Real services select a tree; injected launcher-only services retain local imports."""
    from contextlib import nullcontext
    import plangraph
    import sys
    env = os.environ.copy()
    if getattr(service, 'fresh_tree', False):
        return env, service.code_tree(service.state)
    root = str(Path(plangraph.__file__).resolve().parent.parent)
    try:
        installed = subprocess.run([sys.executable, '-I', '-c', 'import plangraph'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        installed = False
    checkout = (any((Path(root).parent / name).is_file() for name in ('pyproject.toml', 'setup.cfg'))
                or (Path(root).name == 'src' and (Path(root).parent / '.git').exists()))
    inherited = [part for part in env.get('PYTHONPATH', '').split(os.pathsep) if part]
    if checkout:
        env['PYTHONPATH'] = os.pathsep.join([root, *inherited])
    elif not installed:
        env['PYTHONPATH'] = os.pathsep.join([*inherited, root])
    elif 'PYTHONPATH' in env:
        env['PYTHONPATH'] = os.pathsep.join(inherited)
        if not env['PYTHONPATH']:
            del env['PYTHONPATH']
    return env, nullcontext(None)


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
    def git(*args, check=True, failure='run_worktree_create_failed'):
        try:
            return subprocess.run(['git', '-C', str(repo), *args], check=check,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
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
    git('fetch', '--no-write-fetch-head', 'origin', '+refs/heads/main:refs/remotes/origin/main',
        failure='run_worktree_fetch_failed')
    main = git('rev-parse', '--verify', 'refs/remotes/origin/main^{commit}',
               failure='run_worktree_base_unavailable').stdout.strip()
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
