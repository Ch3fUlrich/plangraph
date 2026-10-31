"""D-3287 round 4 on PR #257: git guard shell keys, final-attempt reason,
base-path run registry, orphaned run group kill (seat run-e6d5c597 MEDs)."""
import pytest

from plangraph.spawner.git_guard import DANGEROUS


@pytest.mark.parametrize('key', ['core.editor', 'core.pager', 'pager.log', 'alias.co',
                                 'sequence.editor', 'diff.external', 'diff.x.textconv',
                                 'filter.lfs.smudge', 'remote.origin.uploadpack', 'gpg.ssh.program'])
def test_shell_run_git_keys_are_dangerous(key):
    assert DANGEROUS.fullmatch(key)


@pytest.mark.parametrize('key', ['user.name', 'branch.main.remote', 'diff.tool', 'remote.origin.url'])
def test_plain_git_keys_stay_benign(key):
    assert not DANGEROUS.fullmatch(key)
