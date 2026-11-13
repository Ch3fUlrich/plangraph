"""Fetched-origin ancestry admission; only a named card decision waives an old base."""
import asyncio
import os
import re
import subprocess
import time

ADMISSION_TIMEOUT = 180
def _git_env():
    return {k: v for k, v in os.environ.items()
            if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')}
def _check(repo, default=None, fetch=True, timeout=60, ref='HEAD'):
    repo = os.fspath(repo)
    def git(*args, check=True):
        return subprocess.run(['git', '-C', repo, *args], env=_git_env(), check=check,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    result = dict(ok=False, reason='base_stale: git_failed', repo=repo,
                  default_branch=default, origin_sha=None, behind=None, base_sha=None)
    fetching = False
    try:
        if default is None:
            symbolic = git('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD', check=False)
            default = symbolic.stdout.strip().removeprefix('refs/remotes/origin/') if symbolic.returncode == 0 else 'main'
        result['default_branch'] = default
        if not isinstance(default, str) or default.startswith('-'): return result
        git('check-ref-format', 'refs/heads/' + default)
        if fetch:
            fetching = True
            refspec = '+refs/heads/' + default + ':refs/remotes/origin/' + default
            try:
                git('fetch', '-q', 'origin', refspec)
            except subprocess.CalledProcessError:
                time.sleep(0.1)  # A parallel fetch may briefly hold the tracking ref lock.
                git('fetch', '-q', 'origin', refspec)
            fetching = False
        origin = git('rev-parse', '--verify', 'refs/remotes/origin/' + default + '^{commit}').stdout.strip()
        base = git('rev-parse', '--verify', '--end-of-options', str(ref) + '^{commit}').stdout.strip()
        behind = int(git('rev-list', '--count', base + '..' + origin).stdout.strip())
        result.update(origin_sha=origin, base_sha=base, behind=behind)
        merged = git('merge-base', base, origin, check=False)
        if merged.returncode > 1: raise ValueError('merge-base failed')
        ok = behind == 0 and merged.stdout.strip() == origin
        result.update(ok=ok, reason=None if ok else f'base_stale: behind={behind}')
    except (OSError, ValueError, subprocess.SubprocessError):
        result['reason'] = 'base_stale: fetch_failed' if fetching else 'base_stale: git_failed'
    return result
def base_fresh(repo, *, default=None, fetch=True, timeout=60) -> dict:
    """Compare the run cwd's HEAD with origin's default branch, fetching by default."""
    return _check(repo, default, fetch, timeout)

def fresh_worktree_base(repo, *, base='origin/main', default='main', fetch=True):
    """Fetch, fast-forward main safely, and refuse a stale worktree creation base."""
    from .git_guard import run_worktree_lock
    from pathlib import Path
    from plangraph.routing_errors import SpawnRefused
    def git(*args, check=True):
        return subprocess.run(['git', '-C', os.fspath(repo), *map(str, args)],
            env=_git_env(), capture_output=True, text=True, check=check, timeout=30)
    step = 'worktree_git_failed'
    repo = os.path.abspath(repo)
    try:
        if default != 'main' and not default.startswith('phase/'):
            raise SpawnRefused('base_stale: invalid_default')
        git('check-ref-format', 'refs/heads/' + default)
        common = Path(git('rev-parse', '--git-common-dir').stdout.strip())
        if not common.is_absolute(): common = Path(repo) / common
        with run_worktree_lock(common / 'fresh-base'):
            if fetch:
                step = 'worktree_fetch_failed'
                refs = dict.fromkeys(('main', default))
                git('fetch', '--no-write-fetch-head', 'origin', *(
                    '+refs/heads/' + name + ':refs/remotes/origin/' + name for name in refs))
                step = 'worktree_git_failed'
            base_sha = git('rev-parse', '--verify', '--end-of-options', str(base) + '^{commit}').stdout.strip()
            target = git('rev-parse', '--verify', 'refs/remotes/origin/' + default + '^{commit}').stdout.strip()
            main = git('rev-parse', '--verify', 'refs/remotes/origin/main^{commit}').stdout.strip()
            if any(git('merge-base', '--is-ancestor', sha, base_sha, check=False).returncode for sha in (target, main)):
                raise SpawnRefused('base_stale: worktree_base_behind_origin')
            old = git('rev-parse', '--verify', 'refs/heads/main', check=False)
            if old.returncode == 0:
                old = old.stdout.strip()
                if git('merge-base', '--is-ancestor', old, main, check=False).returncode:
                    raise SpawnRefused('base_stale: main_diverged')
                main_tree = None
                for line in git('worktree', 'list', '--porcelain').stdout.splitlines():
                    if line.startswith('worktree '): main_tree = line.removeprefix('worktree ')
                    elif line == 'branch refs/heads/main':
                        if git('-C', main_tree, 'status', '--porcelain').stdout:
                            raise SpawnRefused('base_stale: main_dirty')
                        git('-C', main_tree, 'merge', '--ff-only', '--no-edit', main)
                        break
                else:
                    git('update-ref', 'refs/heads/main', main, old)
            return base_sha
    except SpawnRefused:
        raise
    except subprocess.TimeoutExpired:
        raise SpawnRefused('base_stale: worktree_fetch_timeout' if step == 'worktree_fetch_failed'
                           else 'base_stale: worktree_git_timeout') from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SpawnRefused('base_stale: ' + step) from None

async def admit_base(service, record, card, *, launch=False):
    """Check the worker checkout and any reviewed diff at both admission boundaries."""
    card = card or {}
    repo = record.get('cwd') or getattr(service.runner, 'default_cwd', None) or os.getcwd()
    result = await asyncio.to_thread(base_fresh, repo)
    decision = card.get('base_decision')
    def accepted(check):
        return check['ok'] or (isinstance(decision, str) and re.fullmatch(r'D-[0-9]+', decision) is not None
            and check.get('behind') is not None and check['behind'] > 0
            and check['reason'] == f"base_stale: behind={check['behind']}")
    diff = card.get('diff_ref') if record.get('role') in ('review', 'judge') else None
    if accepted(result) and diff is not None:
        reviewed = await asyncio.to_thread(_check, diff.get('repo') or repo, fetch=False, ref=diff.get('head', ''))
        if not accepted(reviewed):
            result = reviewed
    if accepted(result):
        record['base_sha'] = card['base_sha'] = result['origin_sha']
        record['checked_base_sha'] = card['checked_base_sha'] = result.get('base_sha')
        record['card'] = dict(card)
        if isinstance(decision, str) and re.fullmatch(r'D-[0-9]+', decision): record['base_decision'] = decision
        if launch:
            record['base_admitted'] = True
            service._save(record)
        return True
    from .item_key import annotate_item
    annotate_item(record, card)
    record.update(status='failed', reason=result['reason'], error=result['reason'],
                  behind=result['behind'], exit_code=1, ended=time.time())
    if record.get('run_worktree'): await asyncio.to_thread(_remove_refused_worktree, service, record)
    service._save(record)
    return False
def _remove_refused_worktree(service, record):
    """Remove only this unlaunched attempt; never force dirty or locked evidence."""
    from pathlib import Path
    from .git_guard import run_worktree_lock, selected_checkout
    metadata = record['run_worktree']
    repo = selected_checkout(service, record)
    try:
        with run_worktree_lock(Path(metadata['path']).parent):
            subprocess.run(['git', '-C', repo, 'worktree', 'remove', metadata['path']],
                env=_git_env(), check=True, capture_output=True, timeout=30)
        metadata['removed'] = True
    except (OSError, subprocess.SubprocessError) as exc:
        record['worktree_cleanup_error'] = type(exc).__name__
def spawn_runs(record):
    """Flatten seat and writer envelopes while retaining the leaf record objects."""
    children = record.get('seats') or record.get('writers')
    return [run for child in children for run in spawn_runs(child)] if children else [record]

def spawn_exit_code(record):
    for run in spawn_runs(record):
        if run.get('status') not in ('running', 'dry_run', 'completed'):
            return run.get('exit_code') or 1
    return 0

async def admitted_spawn(service, payload):
    """The detached handshake acknowledges admission, not mere task creation."""
    prior_tasks = set(service.tasks)
    async def wait():
        record = await service.spawn(**payload, wait_seconds=0)
        runs = spawn_runs(record)
        while any(r.get('status') == 'running' and not r.get('base_admitted')
                  and not r.get('overflow_remote') for r in runs):
            await asyncio.sleep(.01)
            for run in runs:
                if run.get('status') == 'running' and not run.get('overflow_remote'):
                    run.update(service.result(run['run_id']))
        return record
    try:
        return await asyncio.wait_for(wait(), ADMISSION_TIMEOUT)
    except asyncio.TimeoutError:
        owned = {key: task for key, task in service.tasks.items() if key not in prior_tasks}
        for run_id, task in owned.items():
            run = service.result(run_id)
            if run.get('status') == 'running':
                await service.cancel(run_id)  # stop already-admitted workers, not just their supervisors
                run = service.result(run_id)
                if run.get('reason') != 'kill_incomplete':
                    if run.get('run_worktree') and not run.get('base_admitted'):
                        await asyncio.to_thread(_remove_refused_worktree, service, run)
                    service._save(dict(run, status='failed', reason='base_admission_timeout', error='base_admission_timeout', exit_code=1, ended=time.time()))
            if not task.done(): task.cancel()
        await asyncio.gather(*owned.values(), return_exceptions=True)
        return service.save_refusal(ValueError('base_admission_timeout'), payload.get('role', 'implement'),
                                    card=payload.get('card'), reason='base_admission_timeout')
