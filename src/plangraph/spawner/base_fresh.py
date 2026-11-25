"""Fetched-origin ancestry admission; only a named card decision waives an old base."""
import asyncio
import os
import re
import subprocess
import time
from contextvars import ContextVar

ADMISSION_TIMEOUT = 180
#: time.monotonic() at the start of the admission wait of the current spawn (set by admitted_spawn).
ADMISSION_STARTED = ContextVar('admission_started', default=None)
def _git_env():
    from .prompt_input import checkout_env
    return checkout_env()
def _check(repo, default=None, fetch=True, timeout=60, ref='HEAD', *, conflict_base=None):
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
            # A clone of a lane checkout names a lane branch the remote lacks: unknown, not the default.
            if default != 'main' and fetch and not default.startswith('-') \
                    and git('ls-remote', '--exit-code', '--heads', 'origin', 'refs/heads/' + default, check=False).returncode == 2:
                default = 'main'
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
        if conflict_base is not None:
            admitted, observed = conflict_base
            if git('merge-base', '--is-ancestor', admitted, observed, check=False).returncode:
                return result
            if origin == observed:
                origin = admitted
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
def base_fresh(repo, *, default=None, fetch=True, timeout=60, ref='HEAD') -> dict:
    """Compare `ref` (default: the run cwd's HEAD) with origin's default branch, fetching by default."""
    return _check(repo, default, fetch, timeout, ref)

def fresh_worktree_base(repo, *, base='origin/main', default='main', fetch=True):
    """Fetch, fast-forward main safely, and refuse a stale worktree creation base."""
    from .git_guard import run_worktree_lock
    from pathlib import Path
    from plangraph.routing_errors import SpawnRefused
    def git(*args, check=True):
        return subprocess.run(['git', '-C', os.fspath(repo), *map(str, args)],
            env=_git_env(), capture_output=True, text=True, encoding='utf-8', errors='replace', check=check, timeout=30)
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
            if old.returncode == 0 and old.stdout.strip() != main:
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

def _decision_waives(decision, check):
    """Read a scoped operator waiver from the configured, fetched decisions authority."""
    path = os.environ.get('PLANGRAPH_BASE_DECISIONS_FILE')
    if not path or not isinstance(decision, str) or not re.fullmatch(r'D-[0-9]+', decision):
        return False
    from plangraph.keeper.evidence_decisions import DecisionsAdapter, _decision_cell
    try:
        rows = DecisionsAdapter(path, from_origin=True)._rows_strict().get(
            DecisionsAdapter._canonical(decision), [])
        if len(rows) != 1:
            return False
        cell = _decision_cell(rows[0])
        return (re.search(r'(?<![\w-])Source: operator(?=\s|$)', cell) is not None
                and re.search(r'(?<![\w-])Waives: spawn-base:' + re.escape(check['base_sha']) + r'(?=\s|$)', cell) is not None
                and re.search(r'(?<![\w-])Origin: ' + re.escape(check['origin_sha']) + r'(?=\s|$)', cell) is not None)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return False

def _native_review_base(service, record, card):
    """Read the owning writer's exact-head evidence, outside the submitted card."""
    from .item_key import native_writer_review
    run_id = native_writer_review(card)
    diff = card.get('diff_ref')
    if not run_id or record.get('role') != 'review' or not isinstance(diff, dict):
        return None
    try:
        writer = service.result(run_id)
    except (AttributeError, OSError, ValueError):
        return None
    if (writer.get('run_id') != run_id or writer.get('run_kind') != 'native-codex'
            or writer.get('role') != 'implement' or writer.get('status') != 'running'
            or writer.get('base_admitted') is not True or writer.get('overflow_remote')
            or writer.get('origin_receipt')):
        return None
    review = writer.get('review_base') or {}
    checks = writer.get('fast_checks') or {}
    previous = writer.get('review') or {}
    head, base = diff.get('head'), review.get('base')
    if not all(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha)
               for sha in (head, base, review.get('main'), review.get('observed_main'))):
        return None
    delta = previous.get('head') if previous.get('verdict') == 'REQUEST_CHANGES' else None
    if (head != writer.get('final_head') or head != review.get('head')
            or checks != {'head': head, 'status': 'passed'}
            or base != writer.get('base_sha')
            or diff.get('base') not in (base, delta) or not diff.get('base')
            or review.get('mode') not in ('current', 'rebased', 'conflict')):
        return None
    if not all(isinstance(path, str) and os.path.isabs(path)
               for path in (diff.get('repo'), writer.get('repo'))):
        return None
    if os.path.normcase(os.path.realpath(diff['repo'])) != os.path.normcase(os.path.realpath(writer['repo'])):
        return None
    return review

async def admit_base(service, record, card, *, launch=False):
    """Check the worker checkout and any reviewed diff at both admission boundaries."""
    card = card or {}
    repo = record.get('review_source') or record.get('cwd') or getattr(service.runner, 'default_cwd', None) or os.getcwd()
    native = await asyncio.to_thread(_native_review_base, service, record, card)
    if native is not None:
        conflict = (native['main'], native['observed_main']) if native['mode'] == 'conflict' else None
        result = await asyncio.to_thread(_check, card['diff_ref']['repo'], default='main',
                                         ref=native['head'], conflict_base=conflict)
    else:
        result = await asyncio.to_thread(base_fresh, repo)
    decision = card.get('base_decision')
    decisions = {}
    async def accepted(check):
        if check['ok']:
            return True
        if (check.get('behind') is None or check['behind'] <= 0
                or check['reason'] != f"base_stale: behind={check['behind']}"):
            return False
        key = (check.get('base_sha'), check.get('origin_sha'))
        if key not in decisions:
            decisions[key] = await asyncio.to_thread(_decision_waives, decision, check)
        return decisions[key]
    diff = card.get('diff_ref') if record.get('role') in ('review', 'judge') else None
    if native is None and await accepted(result) and diff is not None:
        diff_repo = diff.get('repo') or repo
        same_repo = os.path.normcase(os.path.realpath(diff_repo)) == os.path.normcase(os.path.realpath(repo))
        # The cached check reuses the worker check's refs, so it also reuses its resolved default branch.
        reviewed = await asyncio.to_thread(_check, diff_repo, default=result.get('default_branch') if same_repo else None,
                                           fetch=not same_repo, ref=diff.get('head', ''))
        if not await accepted(reviewed):
            result = reviewed
    if await accepted(result):
        if native is not None:
            record['review_base'] = dict(native)
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
    if record.get('run_worktree'): await asyncio.to_thread(_remove_refused_worktree, service, record, remove_branch=True)
    service._save(record)
    return False
def _remove_refused_worktree(service, record, *, remove_branch=False):
    """Remove only this unlaunched attempt; never force dirty or locked evidence."""
    from pathlib import Path
    from .git_guard import run_worktree_lock, selected_checkout
    metadata = record['run_worktree']
    if (not record.get('run_id') or metadata.get('created_by_run_id') != record['run_id']
            or metadata.get('continuation') or record.get('continuation')
            or (record.get('card') or {}).get('continuation') or record.get('base_admitted')):
        return
    repo = selected_checkout(service, record)
    try:
        with run_worktree_lock(Path(metadata['path']).parent):
            subprocess.run(['git', '-C', repo, 'worktree', 'remove', metadata['path']],
                env=_git_env(), check=True, capture_output=True, timeout=30)
        metadata['removed'] = True
        if remove_branch and metadata.get('branch'):
            branch = metadata['branch']
            # Normal branch deletion protects occupied/unmerged refs. Use the
            # fetched upstream as merge authority even with a detached caller.
            subprocess.run(['git', '-C', repo, '-c', f'branch.{branch}.remote=origin',
                '-c', f'branch.{branch}.merge=refs/heads/main', 'branch', '-d', '--', branch],
                env=_git_env(), check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        record['worktree_cleanup_error'] = type(exc).__name__
def spawn_runs(record):
    """Flatten seat and writer envelopes while retaining the leaf record objects."""
    children = record.get('seats') or record.get('writers')
    return [run for child in children for run in spawn_runs(child)] if children else [record]

def usable_exit_code(code):
    """A usable failure code is a nonzero integer; zero, None, a bool or a string is not."""
    return code if isinstance(code, int) and not isinstance(code, bool) and code != 0 else None

def run_settled(run):
    """True once a leaf run is final: the durable `ended` field is set and the run no longer runs.

    A native writer saves its terminal status before the finisher records `ended`, so a status
    alone is provisional. A planned or queued run has no end to wait for."""
    if run.get('status') in ('dry_run', 'queued'):
        return True
    return run.get('status') != 'running' and run.get('ended') is not None

def run_exit_code(run):
    """The one exit rule of a leaf record: 0 for running/planned/completed, else its nonzero code or 1."""
    if run.get('status') in ('running', 'dry_run', 'completed', 'queued'):
        return 0
    return usable_exit_code(run.get('exit_code')) or 1

def spawn_exit_code(record):
    for run in spawn_runs(record):
        code = run_exit_code(run)
        if code:
            return code
    return 0

async def settle_runs(service, record, wait):
    """Bounded durable wait for the leaves of `record`; a leaf still unsettled at the bound keeps its latest saved state.

    SpawnService.wait owns terminal waiting and the remote refresh of forwarded runs."""
    deadline = time.monotonic() + max(0, wait)
    for run in spawn_runs(record):
        if run_settled(run):
            continue
        try:
            run.update(await service.wait(run['run_id'], timeout=max(0, deadline - time.monotonic())))
        except (TimeoutError, ValueError):
            try:
                run.update(service.result(run['run_id']))
            except (OSError, ValueError):
                pass
    return record

async def admitted_spawn(service, payload):
    """The detached handshake acknowledges admission, not mere task creation."""
    # Routing and record creation count against the window; tasks the spawn creates keep this
    # value (they copy the context), the caller's own context is restored afterwards.
    token = ADMISSION_STARTED.set(time.monotonic())
    try:
        return await _admitted_spawn(service, payload)
    finally:
        ADMISSION_STARTED.reset(token)
async def _admitted_spawn(service, payload):
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
                        await asyncio.to_thread(_remove_refused_worktree, service, run, remove_branch=True)
                    service._save(dict(run, status='failed', reason='base_admission_timeout', error='base_admission_timeout', exit_code=1, ended=time.time()))
            if not task.done(): task.cancel()
        await asyncio.gather(*owned.values(), return_exceptions=True)
        return service.save_refusal(ValueError('base_admission_timeout'), payload.get('role', 'implement'),
                                    card=payload.get('card'), reason='base_admission_timeout')
