"""A refused attempt never launches, even when cleanup cannot remove evidence."""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('error', [OSError(), subprocess.CalledProcessError(1, 'git'),
                                 subprocess.TimeoutExpired('git', 30)])
def test_refused_worktree_cleanup_failure_retains_reason(tmp_path, monkeypatch, error):
    from plangraph.spawner import base_fresh
    metadata = dict(path=str(tmp_path / 'attempt-owned'), branch='lane/resume', base_sha='a' * 40)
    record = dict(run_id='run-refused', cwd=metadata['path'], run_worktree=metadata)
    saved = []
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path)), _save=saved.append)
    monkeypatch.setattr(base_fresh, 'base_fresh', lambda repo: dict(
        ok=False, reason='base_stale: behind=2', behind=2))
    def fail(args, **kwargs):
        assert args == ['git', '-C', str(tmp_path), 'worktree', 'remove', metadata['path']]
        raise error
    monkeypatch.setattr(base_fresh.subprocess, 'run', fail)
    assert not asyncio.run(base_fresh.admit_base(service, record, {}))
    assert record['reason'] == 'base_stale: behind=2' and record['exit_code'] == 1
    assert record['worktree_cleanup_error'] == type(error).__name__
    assert 'removed' not in metadata and saved == [record]
