"""Real-Git regressions for fresh worktree admission from a stale caller."""
import asyncio

import pytest

from test_spawner_base_fresh import advance, dispatch, git, repository

pytestmark = pytest.mark.real_base_fresh


@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('detached', [False, True])
@pytest.mark.parametrize('pinned', [False, True])
def test_fresh_worktree_does_not_admit_the_stale_caller(repository, dispatch, hardened, detached, pinned):
    origin, repo = repository
    old = advance(origin, repo)
    if detached:
        git(repo, 'checkout', '--detach', old)
    service, calls = dispatch[0](hardened), dispatch[1]
    options = dict(leg='cx-gpt-6.1-sol-xhigh', override_reason='hermetic admission test') if pinned else {}
    record = asyncio.run(service.spawn({'estimate_tokens': 100}, 'Implement a tested change',
        run_worktree=True, wait_seconds=None, **options))
    expected = git(origin, 'rev-parse', 'main')
    assert record['status'] == 'completed', record
    assert len(calls) == 1
    assert record['run_worktree']['base_sha'] == expected
    assert git(record['cwd'], 'rev-parse', 'HEAD') == expected
    assert git(repo, 'rev-parse', 'HEAD') == (old if detached else expected)


@pytest.mark.parametrize('tracked', [False, True])
def test_current_main_needs_no_fast_forward_even_when_dirty(repository, tmp_path, tracked):
    from plangraph.spawner.base_fresh import fresh_worktree_base
    origin, repo = repository
    path = repo / 'local.txt'
    if tracked:
        path.write_text('committed')
        git(repo, 'add', path.name)
        git(repo, 'commit', '-m', 'tracked file')
        git(repo, 'push', 'origin', 'HEAD:main')
    path.write_text('preserve this local work')
    before = git(repo, 'status', '--porcelain')
    expected = git(origin, 'rev-parse', 'main')
    sha = fresh_worktree_base(repo)
    git(repo, 'worktree', 'add', '--detach', str(tmp_path / 'fresh-tree'), sha)
    assert sha == expected == git(repo, 'rev-parse', 'HEAD')
    assert path.read_text() == 'preserve this local work'
    assert git(repo, 'status', '--porcelain') == before


def test_cancel_during_creation_waits_and_cleans_owned_attempt(repository, dispatch, monkeypatch):
    import threading
    from plangraph.spawner import git_guard
    origin, repo = repository
    service = dispatch[0](False)
    entered, release = threading.Event(), threading.Event()
    create = git_guard.create
    made = []
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        metadata = create(*args, **kwargs)
        made.append(metadata)
        return metadata
    monkeypatch.setattr(git_guard, 'create', blocked)
    async def exercise():
        record = {'run_id': 'run-cancelcreate', 'cwd': str(repo)}
        card = {'lane_branch': 'lane/cancel-create'}
        task = asyncio.create_task(git_guard.prepare_worktree(service, record, card, True))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The creation thread must have finished before cancellation returns.
        assert made
        assert not (git_guard.Path(made[0]['path'])).exists()
        assert 'lane/cancel-create' not in git(repo, 'branch', '--list')
        # A named lane is immediately reusable, not an orphan branch.
        metadata = await git_guard.prepare_worktree(service, {'run_id': 'run-retry', 'cwd': str(repo)}, card, True)
        assert git(metadata['cwd'], 'rev-parse', 'HEAD') == git(origin, 'rev-parse', 'main')
    asyncio.run(exercise())
