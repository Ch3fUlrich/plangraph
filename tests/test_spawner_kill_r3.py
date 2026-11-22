"""PR #324 round 3 (Haiku seat r2 findings V1..V4): one hermetic test per finding, no host pids."""
import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest
from fake_pid import FAKE_PID

from plangraph.spawner import proc_kill, reap as reap_mod
from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService

posix = pytest.mark.skipif(os.name == 'nt', reason='POSIX process groups')

LEADER = ("import subprocess, sys\n"
          "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
          "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
          "print(p.pid, flush=True)\n")


def _job():
    return type('J', (), {'events': [], 'survivors': [], 'status': 'running', 'exit_code': 0})()


def _sleeper():
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)


def _gone(pid):
    st = proc_kill._stat(pid)
    return st is None or st[0] == 'Z'


@posix
def test_v1_dead_root_unproven_ownership_reports_surviving_group_member():
    root = subprocess.Popen([sys.executable, '-c', LEADER], stdout=subprocess.PIPE, start_new_session=True)
    kid = int(root.stdout.readline())
    root.wait()                                             # root dead and reaped; its group still has a member
    try:
        async def run():
            runner = SpawnerRunner()
            runner.jobs['j'] = job = _job()
            await reap_mod.reap(runner, 'j', type('P', (), {'pid': root.pid})())   # no _owned: unproven
            return job
        job = asyncio.run(run())
        assert job.status == 'failed' and kid in job.survivors
        assert any(e['event'] == 'kill_incomplete' for e in job.events)
        assert not _gone(kid)                               # reported only: never signalled without proof
    finally:
        os.kill(kid, signal.SIGKILL)


@posix
def test_v2_kill_recorded_proves_root_by_persisted_start_and_kills_it():
    p = _sleeper()
    try:
        survivors = asyncio.run(SpawnerRunner().kill_recorded(p.pid, proc_kill.root_identity(p.pid)))
        p.wait(timeout=10)
        assert survivors == [] and p.returncode == -signal.SIGKILL
    finally:
        p.kill(); p.wait()


@posix
def test_v2_kill_recorded_unprovable_root_is_reported_not_killed():
    p = _sleeper()
    try:
        assert asyncio.run(SpawnerRunner().kill_recorded(p.pid, None)) == [p.pid]
        assert asyncio.run(SpawnerRunner().kill_recorded(p.pid, 'stale-token')) == [p.pid]
        assert p.poll() is None
    finally:
        p.kill(); p.wait()


@posix
def test_v2_cancel_after_restart_with_live_unprovable_root_is_kill_incomplete_not_cancelled(tmp_path):
    p = _sleeper()
    try:
        async def run():
            service = SpawnService(SpawnerRunner(), tmp_path)
            record = {'run_id': 'r1', 'status': 'running', 'pid': p.pid, 'pid_start': 'stale-token'}
            service.result = lambda *a, **k: record
            service._save = lambda rec: None
            return await service.cancel('r1')
        out = asyncio.run(run())
        assert out['status'] == 'failed' and out['reason'] == 'kill_incomplete' and out['survivors'] == [p.pid]
    finally:
        p.kill(); p.wait()


@posix
def test_v2_pid_reuse_stale_owned_entry_never_refuses_the_real_kill():
    p = _sleeper()
    try:
        async def run():
            runner = SpawnerRunner()
            runner._owned = {'old': dict(pid=p.pid, pid_start='stale', job_object=False),
                             'new': dict(pid=p.pid, pid_start=proc_kill.root_identity(p.pid), job_object=False)}
            await runner._own_kill(p.pid)
        asyncio.run(run())
        p.wait(timeout=10)
        assert p.returncode == -signal.SIGKILL
    finally:
        p.kill(); p.wait()


def test_v2_owned_entry_is_pruned_at_reap():
    async def run():
        runner = SpawnerRunner(tree_killer=lambda pid: None)
        runner.jobs['j'] = _job()
        runner._owned = {'j': dict(pid=FAKE_PID, pid_start=None, job_object=False)}
        await reap_mod.reap(runner, 'j', type('P', (), {'pid': FAKE_PID})())
        return runner
    assert 'j' not in asyncio.run(run())._owned


class _Api:
    def create_job(self): return 7
    def assign(self, handle, pid): pass
    def pids(self, handle): return []
    def terminate(self, handle): pass
    def close(self, handle): pass


class _Child:
    pid, returncode = 4242, 0

    async def communicate(self, input=None): return b'', b''
    async def wait(self): return 0


def _nt_with_pre_assign_child(monkeypatch, kills):
    live = {5000}
    monkeypatch.setattr(proc_kill, '_nt_table', lambda: {4242: os.getpid(), 5000: 4242})
    monkeypatch.setattr(proc_kill, '_nt_alive', lambda pid: pid in live)

    async def fake_kill(pid, **kw):
        kills.append(sorted(kw.get('members') or ()))
        if fake_kill.dies:
            live.difference_update(kw.get('members') or ())

    fake_kill.dies = True
    monkeypatch.setattr(reap_mod, 'kill_process_tree', fake_kill)

    async def factory(*a, **k): return _Child()
    runner = SpawnerRunner(process_factory=factory)
    runner.platform, runner.win_job_api = 'nt', _Api()
    return runner, fake_kill


@pytest.mark.parametrize('dies', [True, False])
def test_v2_nt_descendant_created_before_job_assign_is_killed_and_verified(monkeypatch, dies):
    kills = []
    runner, fake_kill = _nt_with_pre_assign_child(monkeypatch, kills)
    fake_kill.dies = dies
    monkeypatch.setattr(proc_kill, 'KILL_WAIT_S', 0.3)
    monkeypatch.setattr(reap_mod, 'finish', lambda *a, **k: proc_kill.finish(*a, **{**k, 'wait_s': 0.3}))
    job = asyncio.run(runner.spawn('test', wait_seconds=1, model_or_combo='mimo/mimo-v2.6-flash'))
    asyncio.run(asyncio.wait_for(runner._tasks[job.job_id], 10)) if job.job_id in runner._tasks else None
    assert [5000] in kills                                  # the pre-assign descendant is killed, not ignored
    if dies:
        assert job.status != 'failed' or job.survivors == []
    else:
        assert job.status == 'failed' and 5000 in job.survivors


@pytest.mark.skip(reason='V2 LOW: a setsid() descendant orphaned after its root exits and born <0.25s ago is invisible '
                         'to the /proc snapshot; closing it needs a subreaper (PR_SET_CHILD_SUBREAPER) or a cgroup, pre-existing')
def test_v2_low_setsid_orphan_after_root_exit_is_killed():
    raise AssertionError('needs subreaper/cgroup')


@posix
def test_v4_kill_incomplete_early_return_closes_the_child_transport(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '0.2')
    monkeypatch.setattr(SpawnerRunner, 'identity_provider', staticmethod(lambda pid, **k: None), raising=False)
    box = {}

    async def factory(*a, **k):
        box['p'] = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(60)', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        return box['p']

    async def run():
        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn('test', wait_seconds=0, model_or_combo='mimo/mimo-v2.6-flash')
        await asyncio.wait_for(runner._tasks[job.job_id], 10)
        return job, [box['p']._transport.get_pipe_transport(fd).is_closing() for fd in (1, 2)]
    try:
        job, closing = asyncio.run(run())
        assert job.status == 'failed' and job.survivors == [box['p'].pid]
        assert closing == [True, True]
    finally:
        os.kill(box['p'].pid, signal.SIGKILL)
