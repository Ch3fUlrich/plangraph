"""SPAWNER-DISPATCH-FAIL-DETECT (D-3605): the spawner kills only its own tree and proves it."""
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from plangraph.spawner import proc_kill, reap as reap_mod, runner as runner_mod
from plangraph.spawner.notify import FileInbox
from plangraph.spawner.runner import SpawnerRunner, spawn_service

posix = pytest.mark.skipif(os.name == 'nt', reason='POSIX process groups')

# Root: plain grandchild + a grandchild that setsid()s away from the run's group.
TREE = (
    "import os, subprocess, sys, time\n"
    "kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "a = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kw)\n"
    "b = subprocess.Popen([sys.executable, '-c', 'import os, time; os.setsid(); time.sleep(60)'], **kw)\n"
    "open(sys.argv[1], 'w').write('%d %d' % (a.pid, b.pid))\n"
    "time.sleep(60)\n")


def _gone(pid):
    st = proc_kill._stat(pid)
    return st is None or st[0] == 'Z'


def _wait(path):
    deadline = time.monotonic() + 10
    while not path.exists() or not path.read_text().count(' '):
        assert time.monotonic() < deadline, 'tree did not start'
        time.sleep(0.02)
    return [int(x) for x in path.read_text().split()]


def _reap_all(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@posix
def test_kills_own_tree_including_setsid_grandchild_and_spares_foreign(tmp_path):
    async def run():
        mine, foreign_file = tmp_path / 'mine', tmp_path / 'foreign'
        root = await asyncio.create_subprocess_exec(sys.executable, '-c', TREE, str(mine),
                                                    start_new_session=True)
        foreign = subprocess.Popen([sys.executable, '-c', TREE, str(foreign_file)], start_new_session=True)
        pids = []
        try:
            kids = _wait(mine)
            fkids = _wait(foreign_file)
            pids = [root.pid, *kids, foreign.pid, *fkids]
            ident = proc_kill.root_identity(root.pid)
            assert ident is not None
            members = proc_kill.snapshot(root.pid, ident)
            assert set(members) >= {root.pid, *kids}   # the setsid() grandchild is reached via its parent
            assert foreign.pid not in members and not set(fkids) & set(members)
            await proc_kill.kill_process_tree(root.pid, ident=ident)
            await proc_kill.finish(root.pid, ident, members)
            assert all(_gone(p) for p in [root.pid, *kids])
            assert not any(_gone(p) for p in [foreign.pid, *fkids])   # foreign process: untouched
            # a pid not proven ours is never snapshotted
            assert proc_kill.root_identity(os.getppid()) is None
            assert proc_kill.snapshot(os.getppid(), None) == {}
        finally:
            _reap_all(pids)
            foreign.wait()
            await root.wait()
    asyncio.run(run())


@posix
def test_survivors_fail_run_without_fallthrough_or_second_writer(tmp_path, monkeypatch):
    pidfile = tmp_path / 'grandchild.pid'
    script = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n")
    calls = []

    async def factory(*args, **kwargs):
        calls.append(kwargs.get('cwd'))
        return await asyncio.create_subprocess_exec(sys.executable, '-c', script, **kwargs)

    async def nokill(*args, **kwargs):   # a kill that does nothing: the grandchild survives
        return None

    async def quick_finish(*args, **kwargs):
        return await proc_kill.finish(*args, **{**kwargs, 'wait_s': 0.3})

    monkeypatch.setattr(proc_kill, 'kill_process_tree', nokill)
    monkeypatch.setattr(reap_mod, 'finish', quick_finish)

    async def run():
        service = spawn_service(SpawnerRunner(process_factory=factory, tree_killer=nokill), tmp_path,
                                keeper_sink=FileInbox(tmp_path))
        try:
            record = await service.spawn(card={'estimate_tokens': 1000}, wait_seconds=None)
        finally:
            if pidfile.exists():
                _reap_all([int(pidfile.read_text())])
        return record
    record = asyncio.run(run())
    assert record['status'] == 'failed' and record['reason'] == 'kill_incomplete'
    assert record['survivors'] == [int(pidfile.read_text())]
    assert record['pid_start'] and record['job_object'] is False
    assert len(calls) == 1                                  # no fall-through leg, no second writer
    assert any(a.get('skip_reason') == 'kill_incomplete' for a in record['attempts'])
    events = [json.loads(x) for x in (tmp_path / 'inbox' / 'parent.jsonl').read_text().splitlines()]
    assert len(events) == 1 and events[0]['event'] == 'FAILED' and events[0]['reason'] == 'kill_incomplete'


def test_nt_taskkill_per_snapshot_member_and_msys_winpid(tmp_path):
    fake = tmp_path / 'proc'
    for pid, win in ((100, 5100), (101, 5101)):
        (fake / str(pid)).mkdir(parents=True)
        (fake / str(pid) / 'winpid').write_text(str(win) + '\n')
    cmds = []

    class Proc:
        async def communicate(self, input=None):
            return b'', b''

    async def factory(*argv, **kw):
        cmds.append(argv)
        return Proc()

    table = {100: os.getpid(), 101: 100, 102: 1}
    ident = 'nt'
    members = proc_kill.snapshot(100, ident, platform='nt', nt_table=lambda: table)
    assert set(members) == {100, 101}                       # ppid closure; 102 is foreign
    assert proc_kill.snapshot(100, ident, platform='nt', job_pids=[7, 8], nt_table=lambda: {}) == {7: None, 8: None}
    assert proc_kill.snapshot(100, None, platform='nt', nt_table=lambda: table) == {}   # not proven ours
    asyncio.run(proc_kill.kill_process_tree(100, platform='nt', command_factory=factory,
                                            members=members, proc_root=str(fake)))
    assert cmds == [('taskkill', '/PID', '5100', '/T', '/F'), ('taskkill', '/PID', '5101', '/T', '/F')]


def test_no_shell_wrapper_in_spawner_sources():
    banned = [re.compile(p) for p in (
        r'shell\s*=\s*True', 'create_subprocess_' + 'shell', r'os\.' + 'system', r'''['"]sh['"]\s*,\s*['"]-c['"]''')]
    root = Path(runner_mod.__file__).parent
    hits = [(p.name, rx.pattern) for p in sorted(root.glob('*.py')) for rx in banned
            if rx.search(p.read_text())]
    assert hits == []


def test_spawn_tolerates_process_without_pid(tmp_path):
    class NoPid:
        returncode = 0

        async def communicate(self, input=None):
            return b'', b''

    async def factory(*args, **kwargs):
        return NoPid()

    async def run():
        runner = SpawnerRunner()
        runner.process_factory = factory
        job = await runner.spawn('test', wait_seconds=3)
        assert job.job_id not in getattr(runner, '_owned', {})
        assert job.status != 'failed'
    asyncio.run(run())


class _Job:
    def __init__(self, fail=None, live=()):
        self.fail, self.live, self.calls = fail, list(live), []

    def create_job(self):
        self.calls.append('create')
        if self.fail == 'create':
            raise OSError('create')
        return 7

    def assign(self, handle, pid):
        self.calls.append('assign')
        if self.fail == 'assign':
            raise OSError('assign')

    def pids(self, handle):
        return list(self.live)

    def terminate(self, handle): self.calls.append('terminate')
    def close(self, handle): self.calls.append('close')


class _Fake:
    returncode = 0

    def __init__(self, pid):
        self.pid = pid

    async def communicate(self, input=None):
        return b'', b''


def _nt_runner(monkeypatch, api, table=None):
    async def factory(*a, **k):
        return _Fake(4242)

    runner = SpawnerRunner(process_factory=factory)
    runner.platform, runner.win_job_api = 'nt', api
    monkeypatch.setattr(proc_kill, '_nt_table', lambda: table if table is not None else {4242: os.getpid()})
    monkeypatch.setattr(proc_kill, '_nt_alive', lambda pid: pid in api.live)
    return runner


@pytest.mark.parametrize('step', ['create', 'assign'])
def test_nt_job_failure_refuses_run(tmp_path, monkeypatch, step):
    api = _Job(fail=step)
    runner = _nt_runner(monkeypatch, api)
    job = asyncio.run(runner.spawn('test', wait_seconds=1))
    assert job.status == 'failed' and job.survivors == [4242]
    assert job.job_id not in runner._tasks                 # no monitor: the run never starts
    assert any(e['event'] == 'win_job_unavailable' for e in job.events)


def test_unproven_ownership_never_reaches_killer(monkeypatch):
    async def run():
        killed = []
        runner = SpawnerRunner(tree_killer=lambda pid: killed.append(pid))
        runner.jobs['j'] = job = type('J', (), {'events': [], 'survivors': [], 'status': 'running', 'exit_code': 0})()
        monkeypatch.setattr(SpawnerRunner, 'identity_provider', staticmethod(lambda *a, **k: None))   # r3: the ownership seam
        monkeypatch.setattr(reap_mod, 'alive', lambda *a, **k: True)   # a live root we cannot prove ours
        runner._owned = {'j': dict(pid=4242, pid_start=None, job_object=False)}
        await runner._reap_process('j', _Fake(4242))
        return killed, job
    killed, job = asyncio.run(run())
    assert killed == [] and job.survivors == [4242] and job.status == 'failed'


def test_nt_empty_snapshot_no_taskkill_kill_incomplete():
    cmds = []

    async def factory(*argv, **kw):
        cmds.append(argv)
    with pytest.raises(proc_kill.KillIncomplete):
        proc_kill.snapshot(100, 'nt', platform='nt', nt_table=lambda: {})
    with pytest.raises(proc_kill.KillIncomplete):
        asyncio.run(proc_kill.kill_process_tree(100, platform='nt', command_factory=factory, members={}))
    assert cmds == []


def test_nt_snapshot_failure_in_verify_is_kill_incomplete(monkeypatch):
    monkeypatch.setattr(proc_kill, '_nt_table', lambda: {})
    with pytest.raises(proc_kill.KillIncomplete):
        asyncio.run(proc_kill.verify(100, 'nt', {}, platform='nt', wait_s=0.2))

    def boom():
        raise OSError('job query')
    with pytest.raises(proc_kill.KillIncomplete):
        asyncio.run(proc_kill.verify(100, 'nt', {}, platform='nt', wait_s=0.2, live_pids=boom))


def test_nt_late_job_member_is_reterminated_and_survivor_reported():
    live = [9]
    terminated = []

    def term():
        terminated.append(1)
        if len(terminated) > 1:
            live.clear()
    asyncio.run(proc_kill.verify(100, 'nt', {}, platform='nt', wait_s=2, live_pids=lambda: live, reterminate=term))
    assert len(terminated) == 2 and live == []             # late descendant re-enumerated and killed
    with pytest.raises(proc_kill.KillIncomplete) as ei:
        asyncio.run(proc_kill.verify(100, 'nt', {}, platform='nt', wait_s=0.2, live_pids=lambda: [9]))
    assert ei.value.survivors == [9]


def test_nt_run_level_kill_incomplete(tmp_path, monkeypatch):
    api = _Job(live=[4242, 4243])                           # terminate never empties the job
    runner = _nt_runner(monkeypatch, api)
    monkeypatch.setattr(reap_mod, 'finish', lambda *a, **k: proc_kill.finish(*a, **{**k, 'wait_s': 0.3}))

    async def run():
        service = spawn_service(runner, tmp_path, keeper_sink=FileInbox(tmp_path))
        return await service.spawn(card={'estimate_tokens': 1000}, wait_seconds=None)
    record = asyncio.run(run())
    assert record['status'] == 'failed' and record['reason'] == 'kill_incomplete'
    assert record['survivors'] == [4242, 4243] and len(api.calls) >= 3


def test_nt_job_create_failure_through_service_is_terminal(tmp_path, monkeypatch):
    runner = _nt_runner(monkeypatch, _Job(fail='create'))
    legs = []
    orig = runner.process_factory

    async def counting(*a, **k):
        legs.append(1)
        return await orig(*a, **k)
    runner.process_factory = counting

    async def run():
        service = spawn_service(runner, tmp_path, keeper_sink=FileInbox(tmp_path))
        return await service.spawn(card={'estimate_tokens': 1000}, wait_seconds=None)
    record = asyncio.run(run())
    assert record['status'] == 'failed' and record['reason'] == 'kill_incomplete' and len(legs) == 1


def test_posix_killpg_refuses_unowned_group(monkeypatch):
    sent = []
    monkeypatch.setattr(os, 'killpg', lambda *a: sent.append(('pg', a)))
    monkeypatch.setattr(os, 'kill', lambda *a: sent.append(('k', a)))
    for pid, ident in ((0, 5), (1, 5), (-7, 5), (os.getpid(), None), (os.getpid(), 12345),
                       (os.getppid(), proc_kill.root_identity(os.getppid()))):
        with pytest.raises(proc_kill.KillIncomplete):
            asyncio.run(proc_kill.kill_process_tree(pid, ident=ident))
    assert sent == []


def test_pidless_process_not_failed():
    async def run():
        runner = SpawnerRunner(tree_killer=lambda pid: 1 / 0)
        runner.jobs['j'] = job = type('J', (), {'events': [], 'survivors': [], 'status': 'running', 'exit_code': 0})()
        await runner._reap_process('j', type('P', (), {})())
        return job
    job = asyncio.run(run())
    assert job.status == 'running' and job.survivors == []


def test_absent_pid_is_unproven_without_the_fixture():
    assert proc_kill.root_identity(2 ** 22 + 12345) is None   # only test_spawner_hardening_delivery.py fakes ownership


def test_unproven_pid_timeout_ends_kill_incomplete_without_blocking(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '0.05')
    killed = []

    class Hanging(_Fake):
        async def communicate(self, input=None):
            await asyncio.Event().wait()                    # never exits unless killed

    async def run():
        runner = SpawnerRunner(process_factory=lambda *a, **k: asyncio.sleep(0, Hanging(4242)),
                               tree_killer=lambda pid: killed.append(pid))
        monkeypatch.setattr(SpawnerRunner, 'identity_provider', staticmethod(lambda *a, **k: None))   # r3: the ownership seam
        monkeypatch.setattr(reap_mod, 'alive', lambda *a, **k: True)
        job = await runner.spawn('test', wait_seconds=0)
        t0 = time.monotonic()
        await asyncio.wait_for(runner._tasks[job.job_id], 10)
        return job, time.monotonic() - t0
    job, elapsed = asyncio.run(run())
    assert elapsed < 5                                      # bounded: no wait on a process we may not signal
    assert killed == [] and job.status == 'failed' and job.survivors == [4242]
    assert any(e['event'] == 'kill_incomplete' for e in job.events)
