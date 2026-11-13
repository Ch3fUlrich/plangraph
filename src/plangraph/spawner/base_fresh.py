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
async def admit_base(service, record, card, *, launch=False):
    """Admit the reviewed diff initially, then the actual worktree before launch."""
    card = card or {}
    review = record.get('role') in ('review', 'judge')
    diff = card.get('diff_ref') if review else None
    repo = record.get('cwd') or getattr(service.runner, 'default_cwd', None) or os.getcwd()
    result = (dict(ok=True, origin_sha=None, base_sha=None) if review and diff is None and not record.get('run_worktree') else
        await asyncio.to_thread(_check, diff.get('repo') or repo, ref=diff.get('head', ''))
        if diff is not None and not record.get('run_worktree') else await asyncio.to_thread(base_fresh, repo))
    decision = card.get('base_decision')
    waived = (isinstance(decision, str) and re.fullmatch(r'D-[0-9]+', decision) is not None
              and result.get('behind') is not None and result['behind'] > 0
              and result['reason'] == f"base_stale: behind={result['behind']}")
    if result['ok'] or waived:
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
    from .git_guard import run_worktree_lock
    metadata = record['run_worktree']
    repo = getattr(service.runner, 'default_cwd', None) or os.getcwd()
    try:
        with run_worktree_lock(Path(metadata['path']).parent):
            subprocess.run(['git', '-C', repo, 'worktree', 'remove', metadata['path']],
                env=_git_env(), check=True, capture_output=True, timeout=30)
        metadata['removed'] = True
    except (OSError, subprocess.SubprocessError) as exc:
        record['worktree_cleanup_error'] = type(exc).__name__
async def admitted_spawn(service, payload):
    """The detached handshake acknowledges admission, not mere task creation."""
    async def wait():
        record = await service.spawn(**payload, wait_seconds=0)
        runs = record.get('seats') or [record]
        while any(r.get('status') == 'running' and not r.get('base_admitted')
                  and not r.get('overflow_remote') for r in runs):
            await asyncio.sleep(.01)
            runs[:] = [service.result(r['run_id']) for r in runs]
        return record if record.get('seats') else runs[0]
    try:
        return await asyncio.wait_for(wait(), ADMISSION_TIMEOUT)
    except asyncio.TimeoutError:
        for task in service.tasks.values(): task.cancel()
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
        for run in map(service.result, list(service.tasks)):
            if run.get('status') == 'running' and not run.get('base_admitted'):
                service._save(dict(run, status='failed', reason='base_admission_timeout', error='base_admission_timeout', exit_code=1, ended=time.time()))
        return service.save_refusal(ValueError('base_admission_timeout'), payload.get('role', 'implement'),
                                    card=payload.get('card'), reason='base_admission_timeout')
