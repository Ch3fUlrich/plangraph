"""RUN-WORKTREE refusal and failed-handoff regressions; only local Git is used."""
import asyncio
import ast
import inspect
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.routing_errors import SpawnRefused
from plangraph.spawner import git_guard
from plangraph.spawner.service import SpawnService
from test_spawner_run_worktree import git, repository


@pytest.mark.parametrize('sha', [None, '', 'g' * 40, 'a' * 39,
                               *('a' * n for n in range(41, 64)), 'a' * 65])
def test_dependency_sha_shape_refused_before_git(repository, monkeypatch, sha):
    _, repo, root = repository
    run = subprocess.run
    def observed(args, **kwargs):
        assert 'merge-base' not in args, 'malformed SHA must be refused before Git'
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', observed)
    with pytest.raises(SpawnRefused, match='^run_worktree_dependency_not_merged$'):
        git_guard.create(root, repo, 'lane/bad-sha', finished_dependencies=[sha])


@pytest.mark.parametrize('length', [40, 64])
def test_full_sha_formats_reach_ancestry_check(repository, monkeypatch, length):
    _, repo, root = repository
    run, checked = subprocess.run, []
    sha = 'a' * length
    def observed(args, **kwargs):
        if 'merge-base' in args:
            checked.append(args[-2])
            return SimpleNamespace(returncode=0)
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', observed)
    git_guard.create(root, repo, 'lane/full-sha', finished_dependencies=[sha])
    assert checked == [sha]


@pytest.mark.parametrize('branch', ['bad branch', '-bad', 'bad..branch'])
def test_invalid_branch_is_coded_refusal(repository, branch):
    _, repo, root = repository
    with pytest.raises(SpawnRefused, match='^run_worktree_invalid_branch$'):
        git_guard.create(root, repo, branch)
    assert not list(root.glob('attempt-*'))


def test_repeat_branch_is_coded_refusal_and_preserves_first_tree(repository):
    _, repo, root = repository
    first = git_guard.create(root, repo, 'lane/repeated')
    with pytest.raises(SpawnRefused, match='^run_worktree_branch_exists$'):
        git_guard.create(root, repo, 'lane/repeated')
    assert list(root.glob('attempt-*')) == [Path(first['path'])]
    assert git(first['path'], 'rev-parse', 'HEAD') == first['base_sha']


def test_missing_remote_main_is_coded_refusal(repository):
    origin, repo, root = repository
    git(origin, 'symbolic-ref', 'HEAD', 'refs/heads/other')
    git(origin, 'update-ref', '-d', 'refs/heads/main')
    with pytest.raises(SpawnRefused, match='^run_worktree_fetch_failed$'):
        git_guard.create(root, repo, 'lane/no-main')
    assert not list(root.glob('attempt-*'))


@pytest.mark.parametrize('command,exception,code', [
    ('fetch', FileNotFoundError(), 'run_worktree_fetch_failed'),
    ('rev-parse', subprocess.CalledProcessError(128, 'git'), 'run_worktree_base_unavailable'),
    ('worktree', subprocess.CalledProcessError(128, 'git'), 'run_worktree_create_failed'),
    ('worktree', subprocess.TimeoutExpired('git', 30), 'run_worktree_git_timeout'),
])
def test_other_git_failures_are_coded_refusals(repository, monkeypatch, command, exception, code):
    _, repo, root = repository
    run = subprocess.run
    def observed(args, **kwargs):
        if args[3] == command:
            raise exception
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', observed)
    with pytest.raises(SpawnRefused, match='^' + code + '$'):
        git_guard.create(root, repo, 'lane/git-failure')
    assert not list(root.glob('attempt-*'))


def test_missing_continuation_branch_is_coded_refusal(repository):
    _, repo, root = repository
    with pytest.raises(SpawnRefused, match='^run_worktree_branch_missing$'):
        git_guard.create(root, repo, 'lane/missing', continuation=True)
    assert not list(root.glob('attempt-*'))


def test_fetch_timeout_does_not_hold_host_lock(repository, monkeypatch):
    _, repo, root = repository
    run = subprocess.run
    with git_guard.run_worktree_lock(root):
        pass
    def observed(args, **kwargs):
        if 'fetch' in args:
            if os.name != 'nt':
                import fcntl
                with (root / '.lock').open('rb') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert kwargs['timeout'] <= 30
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', observed)
    with pytest.raises(SpawnRefused, match='^run_worktree_fetch_timeout$'):
        git_guard.create(root, repo, 'lane/timeout')
    assert not list(root.glob('attempt-*'))


def test_failed_fixer_does_not_claim_parent_worktree(repository, tmp_path, monkeypatch):
    _, repo, root = repository
    first = git_guard.create(root, repo, 'lane/fixer-failure')
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(repo)), state=tmp_path / 'state')
    parent = dict(run_id='run-parent', role='implement', status='completed', exit_code=0,
                  cwd=first['path'], run_worktree=first,
                  chosen_leg={'model': 'test', 'harness': 'opencode'})
    snapshots, save = [], service._save
    def observed(record):
        snapshots.append(json.loads(json.dumps(record)))
        save(record)
    monkeypatch.setattr(service, '_save', observed)
    def refuse(*args, **kwargs):
        raise SpawnRefused('run_worktree_create_failed')
    monkeypatch.setattr(git_guard, 'create', refuse)
    asyncio.run(service._continue_writer(parent, 'Remaining gaps: finish the test'))
    child = service.result(parent['continuation_run_id'])
    assert child['status'] == 'failed' and child['error'] == 'run_worktree_create_failed'
    for record in [child, *(r for r in snapshots if r['run_id'] == child['run_id'])]:
        assert record.get('cwd') is None
        assert 'run_worktree' not in record and not record.get('worktree_attempts')
    assert parent['run_worktree'] == first and Path(first['path']).is_dir()


def test_worktree_method_spacing():
    lines = Path(inspect.getfile(SpawnService)).read_text().splitlines()
    tree = ast.parse('\n'.join(lines))
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    for method in service.body:
        if isinstance(method, ast.AsyncFunctionDef) and method.name in ('_spawn', 'cancel'):
            assert not lines[method.lineno - 2].strip(), method.name
