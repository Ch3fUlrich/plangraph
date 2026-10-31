"""D-3287 round 4 on PR #257: git guard shell keys, final-attempt reason,
base-path run registry, orphaned run group kill (seat run-e6d5c597 MEDs)."""
import asyncio
import subprocess
import sys
import time

import pytest

from plangraph.spawner.exit_outcome import release_attempts, stop_orphan_group, terminal_reason
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


def test_base_path_registry_is_released(tmp_path):
    runner = type('R', (), {'_tasks': {}})()
    service = SpawnService(runner=runner, state=tmp_path)
    record = {}
    service._running_records.update(a=record, b=record)
    runner._tasks.update(a=1, b=2)
    release_attempts(service, record)
    assert service._running_records == {} and runner._tasks == {}


@pytest.mark.skipif(sys.platform != 'linux', reason='/proc start-time check')
def test_orphaned_run_group_is_stopped_but_never_a_recycled_pid():
    proc = subprocess.Popen(['sleep', '30'], start_new_session=True)
    try:
        asyncio.run(stop_orphan_group({'pid': proc.pid, 'last_activity_ts': time.time() - 100}))
        assert proc.poll() is None   # started after the run's last activity: not the run's process
        asyncio.run(stop_orphan_group({'pid': proc.pid, 'last_activity_ts': time.time()}))
        assert proc.wait(timeout=3) == -9
    finally:
        proc.kill()
        proc.wait()
