"""D-3287 round 4 on PR #257: git guard shell keys, final-attempt reason,
base-path run registry, orphaned run group kill (seat run-e6d5c597 MEDs)."""
import pytest

from plangraph.spawner.exit_outcome import terminal_reason
from plangraph.spawner.git_guard import DANGEROUS


@pytest.mark.parametrize('key', ['core.editor', 'core.pager', 'pager.log', 'alias.co',
                                 'sequence.editor', 'diff.external', 'diff.x.textconv',
                                 'filter.lfs.smudge', 'remote.origin.uploadpack', 'gpg.ssh.program'])
def test_shell_run_git_keys_are_dangerous(key):
    assert DANGEROUS.fullmatch(key)


@pytest.mark.parametrize('key', ['user.name', 'branch.main.remote', 'diff.tool', 'remote.origin.url'])
def test_plain_git_keys_stay_benign(key):
    assert not DANGEROUS.fullmatch(key)


def test_no_changes_on_an_earlier_leg_is_not_the_runs_reason():
    record = {'status': 'completed', 'exit_code': 0, 'reason': 'harness_limit',
              'events': [{'event': 'writer_no_changes'}],
              'attempts': [{'status': 'failed', 'events': [{'event': 'writer_no_changes'}]},
                           {'status': 'completed', 'events': []}]}
    terminal_reason(record)
    assert 'reason' not in record, record
    record['attempts'][-1]['events'] = [{'event': 'writer_no_changes'}]
    terminal_reason(record)
    assert record['reason'] == 'writer_no_changes'
