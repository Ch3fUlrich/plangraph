"""D-3287 round 4 on PR #257: git guard shell keys,
base-path run registry (seat run-e6d5c597 MEDs)."""
import pytest

from plangraph.spawner.exit_outcome import release_attempts
from plangraph.spawner.git_guard import DANGEROUS
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('key', ['core.editor', 'core.pager', 'pager.log', 'alias.co',
                                 'sequence.editor', 'diff.external', 'diff.x.textconv',
                                 'filter.lfs.smudge', 'remote.origin.uploadpack', 'gpg.ssh.program'])
def test_shell_run_git_keys_are_dangerous(key):
    assert DANGEROUS.fullmatch(key)


@pytest.mark.parametrize('key', ['user.name', 'branch.main.remote', 'diff.tool', 'remote.origin.url'])
def test_plain_git_keys_stay_benign(key):
    assert not DANGEROUS.fullmatch(key)


def test_base_path_registry_is_released(tmp_path):
    runner = type('R', (), {'_tasks': {}})()
    service = SpawnService(runner=runner, state=tmp_path)
    record = {}
    service._running_records.update(a=record, b=record)
    runner._tasks.update(a=1, b=2)
    release_attempts(service, record)
    assert service._running_records == {} and runner._tasks == {}
