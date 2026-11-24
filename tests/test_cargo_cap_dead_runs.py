"""Dead run records never keep the shared cargo target busy (LE-CARGO-CAP-CLEANUP-BLOCKED-BY-DEAD-RUN-RECORDS)."""
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from plangraph.spawner import host_ready as h
from plangraph.spawner.exit_outcome import with_stale


def dead_pid():
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    return child.pid


@pytest.fixture
def cap(tmp_path, monkeypatch):
    target = tmp_path / 'shared'
    target.mkdir()
    (target / 'artifact').write_text('cached build')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_CAP_GB', '20')
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 100)
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 20.04)

    def refusal(runs):
        return h.local_disk_refusal(tmp_path / 'checkout', SimpleNamespace(ps=lambda: {'runs': runs}))
    return target, refusal


def test_dead_timed_out_stalled_and_running_records_do_not_block_cleanup(cap):
    target, refusal = cap
    gone = dead_pid()
    runs = [dict(status='timed_out', pid=gone, pid_start=1), dict(status='stalled', pid=gone, pid_start=1),
            dict(status='running', pid=gone, pid_start=1)]
    assert refusal(runs) is None
    assert not target.exists()


def test_a_live_run_keeps_the_target_busy(cap):
    target, refusal = cap
    runs = [dict(status='running', pid=os.getpid(), pid_start=None)]
    assert 'busy' in refusal(runs)
    assert (target / 'artifact').exists()


def test_a_running_record_without_pid_evidence_stays_busy(cap):
    target, refusal = cap
    assert 'busy' in refusal([dict(status='running')])
    assert (target / 'artifact').exists()


def test_a_live_supervisor_keeps_the_target_busy(cap, tmp_path):
    target, refusal = cap
    log = tmp_path / 'supervisor.stderr.log'
    (tmp_path / 'supervisor.stderr.log.pid').write_text(str(os.getpid()))
    runs = [dict(status='running', pid=dead_pid(), pid_start=1, supervisor_stderr_path=str(log))]
    assert 'busy' in refusal(runs)


def test_a_live_build_holding_the_lease_delays_cleanup_and_it_proceeds_after(cap, monkeypatch):
    target, refusal = cap
    monkeypatch.setenv(h.CLEANUP_WAIT_ENV, '10')
    lease = h.cargo_lease()
    seen = []

    def release():
        time.sleep(0.4)
        seen.append((target / 'artifact').exists())   # nothing was deleted under the live build
        lease.close()
    threading.Thread(target=release).start()
    assert refusal([]) is None
    assert seen == [True]
    assert not target.exists()


def test_cleanup_gives_up_with_busy_when_the_lease_outlasts_the_wait(cap, monkeypatch):
    target, refusal = cap
    monkeypatch.setenv(h.CLEANUP_WAIT_ENV, '0.2')
    with h.cargo_lease():
        assert 'cargo_target_cap' in refusal([])
        assert (target / 'artifact').exists()


def test_ps_shows_a_dead_pid_running_record_as_stale():
    live = dict(status='running', pid=os.getpid(), pid_start=None)
    dead = dict(status='running', pid=dead_pid(), pid_start=1)
    assert 'stale' not in with_stale(live)
    shown = with_stale(dead)
    assert shown['stale'] is True and 'pid' in shown['stale_reason'] and shown['status'] == 'running'
    for status in ('completed', 'timed_out', 'stalled'):
        assert 'stale' not in with_stale(dict(dead, status=status))


def test_a_busy_host_over_the_cap_sheds_idle_variants_before_refusing(cap, monkeypatch):
    """LE-WS-DISK-FULL-STALE-WORKTREES-AND-CAP-REFUSALS: busy is a refusal only when trimming did not help."""
    target, refusal = cap
    idle, live = target / 'cli-tests' / 'idle', target / 'cli-tests' / 'live'
    for variant in (idle, live):
        variant.mkdir(parents=True)
        (variant / 'obj').write_text('x')
    old = time.time() - 5 * 3600
    for path in (idle / 'obj', idle):
        os.utime(path, (old, old))
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 20.04 if idle.exists() else 3.0)
    runs = [dict(status='running', pid=os.getpid(), pid_start=None)]
    assert refusal(runs) is None
    assert not idle.exists() and (live / 'obj').exists()
    assert (target / 'artifact').exists()


def test_a_busy_host_with_nothing_idle_still_refuses_as_busy(cap):
    target, refusal = cap
    (target / 'cli-tests' / 'live').mkdir(parents=True)
    runs = [dict(status='running', pid=os.getpid(), pid_start=None)]
    assert 'cargo_target_cap' in refusal(runs) and 'busy' in refusal(runs)
    assert (target / 'cli-tests' / 'live').exists()
