"""Run-tree slice 1: local Git fixtures, no supervisor or record-schema changes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from plangraph.spawner import git_guard


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
        *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def trees(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    origin, repo, state = (tmp_path / name for name in ('origin', 'clone', 'state'))
    origin.mkdir()
    git(origin, 'init', '-b', 'main')
    (origin / 'version').write_text('A')
    git(origin, 'add', '.')
    git(origin, 'commit', '-m', 'tree A')
    git(tmp_path, 'clone', str(origin), str(repo))
    git(repo, 'switch', '-c', 'caller')
    return origin, repo, state


@pytest.fixture
def cached_tree(trees):
    with git_guard.run_tree(trees[2], trees[1]) as tree:
        assert git(trees[1], 'rev-parse', 'HEAD') == git(trees[0], 'rev-parse', 'HEAD')
        return *trees, tree


def advance(origin, version='B'):
    (origin / 'version').write_text(version)
    git(origin, 'commit', '-am', 'tree ' + version)
    return git(origin, 'rev-parse', 'HEAD')


def save_record(state, **row):
    record = state / 'runs/run-a/record.json'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(row))
    return record


def age(path):
    os.utime(path, (1, 1))
    return Path(path)


def test_live_run_keeps_a_new_run_selects_b(cached_tree):
    origin, repo, state, a = cached_tree
    caller = git(repo, 'rev-parse', 'HEAD')
    path = age(a['tree_path'])
    save_record(state, status='running', tree_sha=a['tree_sha'], ended=2)
    main = advance(origin)
    with git_guard.run_tree(state, repo) as b:
        assert b['tree_sha'] == main != a['tree_sha']
        assert Path(b['tree_path']) == state / 'trees' / main
        assert git(b['tree_path'], 'rev-parse', 'HEAD') == main
        assert git(b['tree_path'], 'branch', '--show-current') == ''
        assert (Path(b['tree_path']) / 'version').read_text() == 'B'
        assert (path / 'version').read_text() == 'A'
        assert git(path, 'rev-parse', 'HEAD') == caller
        assert path.stat().st_mtime == 1
        assert git(repo, 'rev-parse', 'HEAD') == caller


@pytest.mark.parametrize('registered', [False, True, 'locked'], ids=['removed', 'unlocked', 'locked'])
@pytest.mark.parametrize('offline', [False, True])
def test_missing_cached_directory_is_created_before_use(cached_tree, registered, offline):
    _, repo, state, previous = cached_tree
    path = Path(previous['tree_path'])
    if registered:
        if registered == 'locked':
            git(repo, 'worktree', 'lock', path)
        shutil.rmtree(path)  # Delete only this temporary tree, retaining registration.
        assert str(path) in git(repo, 'worktree', 'list', '--porcelain')
    else:
        git(repo, 'worktree', 'remove', str(path))
    assert not path.exists()
    assert (path.parent / (previous['tree_sha'] + '.lease')).exists()
    if offline:
        git(repo, 'remote', 'set-url', 'origin', str(repo / 'missing'))
    if offline:
        from plangraph.routing_errors import SpawnRefused
        with pytest.raises(SpawnRefused, match='base_stale: fetch_failed'):
            with git_guard.run_tree(state, repo):
                pytest.fail('offline worktree was created')
        assert not path.exists()
        return
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == previous['tree_sha']
        assert tree['tree_path'] == previous['tree_path']
        assert tree.get('tree_stale') == ('fetch_failed' if offline else None)
        assert git(path, 'rev-parse', 'HEAD') == tree['tree_sha']


@pytest.mark.parametrize('failure', ['error', 'timeout'])
def test_fetch_failure_prefers_verified_origin_main_over_newest_cache(cached_tree, monkeypatch, failure):
    origin, repo, state, a = cached_tree
    main = advance(origin)
    with git_guard.run_tree(state, repo):
        pass
    os.utime(a['tree_path'], (2_000_000_000, 2_000_000_000))
    fake = state / 'trees' / ('0' * 40)
    fake.mkdir()
    os.utime(fake, (2_100_000_000, 2_100_000_000))
    run = subprocess.run
    def fail_fetch(args, **kwargs):
        if 'fetch' in args:
            assert kwargs['timeout'] == 10
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(args, 10)
            raise subprocess.CalledProcessError(1, args)
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', fail_fetch)
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == main
        assert tree['tree_stale'] == ('fetch_timeout' if failure == 'timeout' else 'fetch_failed')
        assert git(tree['tree_path'], 'rev-parse', 'HEAD') == main


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
@pytest.mark.parametrize('row', [dict(status='running'), dict(status='running', tree_sha=None),
    dict(status='needs_input'), dict(status='orphaned'),
    dict(status='pending', ended=2), dict(status='failed', survivors=[123]), {}])
def test_unidentified_nonterminal_record_protects_every_tree(trees, row):
    origin, repo, state = trees
    paths = []
    save_record(state, **row)
    for version in ('B', 'C'):
        with git_guard.run_tree(state, repo) as tree:
            paths.append(age(tree['tree_path']))
        advance(origin, version)
    with git_guard.run_tree(state, repo):
        assert all(path.is_dir() for path in paths)
    save_record(state, status='completed')  # Terminal legacy records do not pin trees.
    with git_guard.run_tree(state, repo):
        assert all(not path.exists() for path in paths)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
@pytest.mark.parametrize('status', ['running', 'needs_input', 'orphaned', 'limited',
    'stalled', 'timed_out', 'terminated_after_answer', 'completed', 'failed', 'cancelled',
    'killed', 'dry_run', 'succeeded', 'timeout', 'refused', 'skipped'])
def test_cleanup_protects_live_records_and_startup_leases(trees, status):
    from plangraph.spawner.runner import is_terminal
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as a:
        path = age(a['tree_path'])
        advance(origin)
        with git_guard.run_tree(state, repo) as b:
            assert path.is_dir()  # A has no durable record yet, but holds its lease.
    b_path = age(b['tree_path'])
    save_record(state, status=status, tree_sha=a['tree_sha'])
    advance(origin, 'C')
    with git_guard.run_tree(state, repo):
        assert path.is_dir() is not is_terminal(status)
        assert not b_path.exists()
    save_record(state, status='completed', tree_sha=a['tree_sha'])
    with git_guard.run_tree(state, repo):
        assert not path.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
@pytest.mark.parametrize('unreadable', [False, True])
def test_corrupt_records_withhold_cleanup_but_not_selection(cached_tree, monkeypatch, unreadable):
    origin, repo, state, a = cached_tree
    path = age(a['tree_path'])
    main = advance(origin)
    record = save_record(state, status='running')
    record.write_text('{')
    read = Path.read_text
    def fail_read(current, *args, **kwargs):
        if current == record.resolve() and unreadable:
            raise PermissionError('cannot read record')
        return read(current, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', fail_read)
    with git_guard.run_tree(state, repo) as b:
        assert b['tree_sha'] == main and path.is_dir()


def test_run_tree_changelog_entry():
    root = Path(__file__).resolve().parents[1]
    entries = [root / 'CHANGELOG.md', *root.glob('changelog.d/*.md')]
    assert any('SPAWNER-RUN-TREE-S1-TREE-PER-SHA' in p.read_text(encoding='utf-8') for p in entries)


def test_parallel_creation_is_idempotent(trees):
    _, repo, state = trees
    barrier = threading.Barrier(2)
    def select(_):
        barrier.wait(timeout=10)
        with git_guard.run_tree(state, repo) as tree:
            return tree
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(select, range(2)))
    assert results[0] == results[1] and git(repo, 'worktree', 'list', '--porcelain').count('worktree ') == 2


def test_slow_creation_does_not_hold_a_host_wide_lock(trees, monkeypatch):
    origin, repo, state = trees
    entered, release = threading.Event(), threading.Event()
    first_sha = git(origin, 'rev-parse', 'HEAD')
    run = subprocess.run
    def slow_add(args, **kwargs):
        if 'worktree' in args and 'add' in args and args[-1] == first_sha:
            assert kwargs['timeout'] == 30
            entered.set()
            assert release.wait(15)
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', slow_add)
    def select():
        with git_guard.run_tree(state, repo) as tree:
            return tree
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(select)
        try:
            assert entered.wait(10)
            main = advance(origin)
            second = pool.submit(select).result(timeout=10)
            assert second['tree_sha'] == main
        finally:
            release.set()
        assert first.result()['tree_sha'] == first_sha


def test_existing_directory_must_have_the_selected_head(cached_tree):
    origin, repo, state, a = cached_tree
    main = advance(origin)
    git(repo, 'fetch', 'origin')
    git(a['tree_path'], 'checkout', '--detach', main)
    git(repo, 'remote', 'set-url', 'origin', str(repo / 'missing'))
    git(repo, 'update-ref', 'refs/remotes/origin/main', a['tree_sha'])
    with pytest.raises(RuntimeError, match='run_tree_invalid_head'):
        with git_guard.run_tree(state, repo):
            pytest.fail('wrong HEAD was admitted')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
def test_cleanup_keeps_dirty_and_young_trees(cached_tree):
    origin, repo, state, a = cached_tree
    (Path(a['tree_path']) / 'untracked').touch()
    age(a['tree_path'])
    advance(origin)
    with git_guard.run_tree(state, repo) as b:
        pass
    advance(origin, 'C')
    with git_guard.run_tree(state, repo):
        assert Path(a['tree_path']).is_dir() and Path(b['tree_path']).is_dir()


def test_windows_selection_disables_reaping_without_importing_fcntl(cached_tree, monkeypatch):
    import builtins
    from types import SimpleNamespace
    origin, repo, state, a = cached_tree
    age(a['tree_path'])
    main = advance(origin)
    calls = []
    monkeypatch.setitem(__import__('sys').modules, 'msvcrt', SimpleNamespace(
        LK_LOCK=1, LK_UNLCK=0, locking=lambda fd, mode, count: calls.append(mode)))
    real_import = builtins.__import__
    def no_fcntl(name, *args, **kwargs):
        if name == 'fcntl':
            raise AssertionError('Windows imported fcntl')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_fcntl)
    monkeypatch.setattr(git_guard, 'os', SimpleNamespace(
        name='nt', environ=os.environ, utime=os.utime, fstat=os.fstat))
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == main and Path(tree['tree_path']).is_dir() and Path(a['tree_path']).is_dir()
    assert calls == [1, 0, 1, 1, 0, 0]


def test_cached_directory_cannot_be_an_unregistered_subdirectory(trees):
    _, repo, state = trees
    state = repo / 'state'
    sha = git(repo, 'rev-parse', 'HEAD')
    (state / 'trees' / sha).mkdir(parents=True)
    with pytest.raises(RuntimeError, match='run_tree_invalid'):
        with git_guard.run_tree(state, repo):
            pytest.fail('plain directory was admitted as a worktree')
