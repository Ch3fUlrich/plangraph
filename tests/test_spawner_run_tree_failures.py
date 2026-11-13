"""Selection, lease lifetime and adapter error contracts; local repositories only."""
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.spawner import git_guard, launch_spec
from test_spawner_run_tree import trees, git, advance


def test_explicit_repository_beats_environment_override(trees, monkeypatch, tmp_path):
    origin, repo, state = trees
    other = tmp_path / 'other'; other.mkdir(); git(other, 'init', '-b', 'main')
    git(other, 'commit', '--allow-empty', '-m', 'unrelated repository')
    monkeypatch.setenv('PLANGRAPH_SPAWNER_REPO', str(other))
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == git(origin, 'rev-parse', 'HEAD')
        assert git(tree['tree_path'], 'rev-parse', '--git-common-dir') == str(repo / '.git')


def test_default_service_and_lock_create_missing_root(tmp_path):
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    service = spawn_service(None, state=tmp_path / 'state')
    assert isinstance(service.runner, SpawnerRunner)
    assert not service.injected_runner and not service.fresh_tree
    root = tmp_path / 'missing/trees'
    assert not root.exists()
    with git_guard.run_worktree_lock(root):
        assert root.is_dir() and (root / '.lock').is_file()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX leases')
@pytest.mark.parametrize('failure', ['flock', 'records', 'unlock'])
def test_selection_exception_closes_lease_before_yield(trees, monkeypatch, failure):
    import fcntl
    _, repo, state = trees
    opened = []
    real_open, flock = Path.open, fcntl.flock
    def observe(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        if path.suffix == '.lease': opened.append(stream)
        return stream
    def fail_lock(stream, operation):
        if (failure == 'flock' and operation == fcntl.LOCK_SH or
                failure == 'unlock' and operation == fcntl.LOCK_UN):
            raise RuntimeError('selection failed')
        return flock(stream, operation)
    real_glob = Path.glob
    def fail_records(path, pattern):
        if failure == 'records' and path == state / 'runs':
            raise RuntimeError('selection failed')
        return real_glob(path, pattern)
    monkeypatch.setattr(Path, 'open', observe)
    monkeypatch.setattr(fcntl, 'flock', fail_lock)
    monkeypatch.setattr(Path, 'glob', fail_records)
    with pytest.raises(RuntimeError, match='selection failed'):
        with git_guard.run_tree(state, repo): pytest.fail('selection yielded after failure')
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.parametrize('row,protected', [
    ({'status': 'running'}, True), ({'status': 'queued', 'ended': 2}, True),
    ({'status': 'completed'}, False), ({'status': 'refused'}, False),
    ({'status': 'failed', 'ended': None}, False),
    *[({'status': status}, False) for status in ('cancelled', 'stalled', 'timeout',
        'limited', 'dry_run', 'needs_input', 'orphaned')],
    ({'status': 'completed', 'survivors': [42]}, True),
    ({'ended': None}, True), ({'status': 'unknown', 'ended': 2}, True),
])
def test_cleanup_recognizes_record_status_without_ended(trees, row, protected):
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as old: pass
    path = Path(old['tree_path']); os.utime(path, (1, 1)); advance(origin)
    record = state / 'runs/run-schema/record.json'; record.parent.mkdir(parents=True)
    record.write_text(json.dumps(dict(old, **row)))
    with git_guard.run_tree(state, repo) as new:
        assert new['tree_sha'] != old['tree_sha']
        assert path.exists() is protected


def test_cleanup_accepts_real_service_refusal_record(trees):
    from plangraph.routing_errors import SpawnRefused
    from plangraph.spawner.service import SpawnService
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as old: pass
    path = Path(old['tree_path']); os.utime(path, (1, 1)); advance(origin)
    service = SpawnService(runner=SimpleNamespace(), state=state)
    refusal = service.save_refusal(SpawnRefused('fixture refusal'), **old)
    saved = json.loads(service._path(refusal['run_id']).read_text(encoding='utf-8'))
    assert saved['status'] == 'refused' and saved['ended'] is not None
    assert saved['tree_sha'] == old['tree_sha']
    with git_guard.run_tree(state, repo):
        assert not path.exists()


@pytest.mark.parametrize('outcome,error', [
    ('timeout', 'TimeoutExpired'), ('oserror', 'OSError'),
    ('invalid-json', 'JSONDecodeError'), ('invalid-shape', 'AttributeError'),
    ('invalid-events', 'AttributeError'), ('tree', 'RuntimeError'),
])
def test_fresh_adapter_failures_keep_launch_spec_contract(tmp_path, monkeypatch, capsys, outcome, error):
    request = dict(run_id='run-' + 'a' * 32, job_id='job', role='review', dry_run=True,
        leg={'id': 'fixture'}, prompt='task', card={}, cwd=str(tmp_path), state=str(tmp_path))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(request)))
    def tree(state):
        if outcome == 'tree': raise RuntimeError('private detail')
        return nullcontext(dict(tree_path=str(tmp_path), tree_sha='a' * 40))
    monkeypatch.setattr(git_guard, 'run_tree', tree)
    def run(*args, **kwargs):
        if outcome == 'timeout': raise subprocess.TimeoutExpired('private detail', 60)
        if outcome == 'oserror': raise OSError('private detail')
        return SimpleNamespace(returncode=0, stdout={'invalid-json': '{',
            'invalid-shape': '[]', 'invalid-events': '{"events":null}'}.get(outcome, '{}'))
    monkeypatch.setattr(subprocess, 'run', run)
    assert launch_spec.fresh_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == f'launch-spec failed: {error}\n'
