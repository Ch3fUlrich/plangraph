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

from plangraph.spawner import proc_kill, runner as runner_mod
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
            await proc_kill.kill_process_tree(root.pid)
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
    monkeypatch.setattr(runner_mod, 'finish', quick_finish)

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
        async def communicate(self):
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
