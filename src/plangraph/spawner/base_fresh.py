"""Fetched-origin ancestry admission; only a named card decision waives an old base."""
import asyncio
import os
import re
import subprocess
import time


def _check(repo, default=None, fetch=True, timeout=60, ref='HEAD'):
    repo = os.fspath(repo)
    env = {k: v for k, v in os.environ.items()
           if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')}
    def git(*args, check=True):
        return subprocess.run(['git', '-C', repo, *args], env=env, check=check,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    result = dict(ok=False, reason='base_stale: git_failed', repo=repo,
                  default_branch=default, origin_sha=None, behind=None, base_sha=None)
    fetching = False
    try:
        if default is None:
            symbolic = git('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD', check=False)
            default = symbolic.stdout.strip().removeprefix('refs/remotes/origin/') if symbolic.returncode == 0 else 'main'
        result['default_branch'] = default
        if not isinstance(default, str) or default.startswith('-'):
            return result
        git('check-ref-format', 'refs/heads/' + default)
        if fetch:
            fetching = True
            git('fetch', '-q', 'origin', default)
            fetching = False
        origin = git('rev-parse', '--verify', 'refs/remotes/origin/' + default + '^{commit}').stdout.strip()
        base = git('rev-parse', '--verify', '--end-of-options', str(ref) + '^{commit}').stdout.strip()
        behind = int(git('rev-list', '--count', base + '..' + origin).stdout.strip())
        result.update(origin_sha=origin, base_sha=base, behind=behind)
        merged = git('merge-base', base, origin, check=False)
        if merged.returncode > 1: raise ValueError('merge-base failed')
        merged = merged.stdout.strip()
        result.update(ok=behind == 0 and merged == origin,
                      reason=None if behind == 0 and merged == origin else f'base_stale: behind={behind}')
    except (OSError, ValueError, subprocess.SubprocessError):
        result['reason'] = 'base_stale: fetch_failed' if fetching else 'base_stale: git_failed'
    return result


def base_fresh(repo, *, default=None, fetch=True, timeout=60) -> dict:
    """Compare the run cwd's HEAD with origin's default branch, fetching by default."""
    return _check(repo, default, fetch, timeout)


async def admit_base(service, record, card):
    """Shared record-creation gate for routed, pinned and seat runs, before launch."""
    card = card or {}
    review = record.get('role') in ('review', 'judge')
    diff = card.get('diff_ref') if review else None
    if review and diff is None:
        return True
    repo = record.get('cwd') or getattr(service.runner, 'default_cwd', None) or os.getcwd()
    if diff is not None:
        result = await asyncio.to_thread(_check, diff.get('repo') or repo, ref=diff.get('head', ''))
    else:
        result = await asyncio.to_thread(base_fresh, repo)
    decision = card.get('base_decision')
    waived = (isinstance(decision, str) and re.fullmatch(r'D-[0-9]+', decision) is not None
              and result.get('behind') is not None and result['behind'] > 0
              and result['reason'] == f"base_stale: behind={result['behind']}")
    if result['ok'] or waived:
        record['base_sha'] = card['base_sha'] = result['origin_sha']
        if isinstance(decision, str) and re.fullmatch(r'D-[0-9]+', decision):
            record['base_decision'] = decision
        return True
    from .item_key import annotate_item
    annotate_item(record, card)
    record.update(status='failed', reason=result['reason'], error=result['reason'],
                  behind=result['behind'], exit_code=1, ended=time.time())
    service._save(record)
    return False
