"""Missing diff repositories and pre-upgrade worktree records fail safely."""
import asyncio

import pytest

from plangraph.routing_errors import ParameterRefused
from plangraph.spawner import overflow, seat_spawn
from plangraph.spawner.runner import SpawnerRunner, spawn_service
from test_spawner_seat_cwd import checkouts, service  # noqa: F401 shared local fixtures


@pytest.mark.parametrize('diff', [{}, {'base': 'abc', 'head': 'def'}, {'repo': None}, {'repo': ''}])
def test_missing_diff_repo_records_parameter_refusal(checkouts, tmp_path, monkeypatch, diff):
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(tmp_path / 'missing.json'))
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 0))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    service = spawn_service(SpawnerRunner(default_cwd=str(checkouts[0])), state=tmp_path / 'state')
    with pytest.raises(ParameterRefused, match='diff_ref.*repo'):
        asyncio.run(service.spawn(card=dict(estimate_tokens=1000, diff_ref=diff),
            prompt='Review', role='review', writer_families=['gpt']))
    runs = service.ps()['runs']
    assert len(runs) == 1 and runs[0]['status'] == 'refused'
    assert runs[0]['refusal'] == 'ParameterRefused' and not service.runner.jobs


def test_legacy_worktree_backfills_default_before_policy(service, checkouts, tmp_path, monkeypatch):
    from plangraph.spawner import git_guard
    from plangraph import settings
    service.runner.default_cwd = str(checkouts[1])
    old = str(tmp_path / 'pruned-attempt')
    record = dict(run_id='run-legacy', cwd=old, run_worktree=dict(path=old, branch='lane/legacy'))
    def policy(repo):
        assert str(repo) == str(checkouts[1])
        return {}
    def create(root, repo, branch, continuation, **kwargs):
        assert str(repo) == str(checkouts[1]) and continuation
        return dict(path=str(tmp_path / 'new-attempt'), branch=branch, base_sha='abc')
    monkeypatch.setattr(settings, '_policy_data', policy)
    monkeypatch.setattr(git_guard, 'create', create)
    asyncio.run(git_guard.prepare_worktree(service, record, {}, True))
    assert record['selected_cwd'] == str(checkouts[1])
