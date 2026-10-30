"""Run watchdog decisions and platform sampling contracts."""
import math
import asyncio
import os
import sys
import time
import pytest

from plangraph.spawner.run_watchdog import Watchdog, thresholds, is_activity, cpu_seconds
from plangraph.planner.routing import SpawnRefused


@pytest.mark.parametrize('value', [0, -1, True, '2', math.inf, math.nan])
@pytest.mark.parametrize('key', ['stall_min', 'first_edit_min', 'max_min'])
def test_bad_card(value, key):
    with pytest.raises(SpawnRefused):
        thresholds({key: value})


def test_activity():
    assert not is_activity('\x1b[0m> build · cx-gpt-6.1-sol')
    assert not is_activity('  ')
    assert is_activity('\x1b[0m$ \x1b[0mgit status')


def test_threshold_defaults_and_override(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '7')
    assert thresholds()['max_s'] == 7
    assert thresholds({'max_min': 2})['max_s'] == 120


def test_cpu_ignored_before_edit():
    w = Watchdog(thresholds({'stall_min': 1}), 0, ('files', 'head'))
    assert w.decide(1, ('files', 'head'), None, 1) is None
    assert w.decide(61, ('files', 'head'), None, 900)['rule'] == 'no-activity'


def test_unavailable_cpu_not_quiet():
    w = Watchdog(thresholds({'stall_min': 1}), 0, ('files', 'head'))
    w.decide(1, ('changed', 'head'), None, None)
    event = w.decide(62, ('changed', 'head'), None, None)
    assert 'cpu' in event['unavailable']
    assert 'cpu' not in event['quiet']


def test_running_record_saved(tmp_path):
    from plangraph.spawner.runner import spawn_service, SpawnerRunner, JobInfo
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    service = spawn_service(runner=runner, state=tmp_path)
    job = JobInfo('j', 'running', '', 'm', stdout='tool output', last_activity_ts=123)
    runner.jobs['j'] = job
    record = dict(run_id='run-abc', job_id='j', status='running', ended=None)
    service._running_records['j'] = record
    runner.on_sample('j')
    saved = service.result('run-abc')
    assert saved['stdout_tail'] == 'tool output'
    assert saved['last_activity_ts'] == 123


def test_refused_before_launch(tmp_path):
    from plangraph.spawner.runner import spawn_service
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    class Runner:
        default_cwd = str(tmp_path)
        jobs = {}
        async def spawn(self, **kwargs):
            pytest.fail('invalid threshold launched a child')
    async def run():
        service = spawn_service(runner=Runner(), state=tmp_path)
        service._route = lambda *a, **kw: SpawnPlan((DEFAULT_LEGS[0],), (), 'test', (), ())
        record = await service.spawn({'estimate_tokens': 1000, 'stall_min': True}, wait_seconds=None)
        assert record['status'] == 'refused'
        assert record['exit_code'] == 2
    asyncio.run(run())


def test_rules():
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 3})
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(60, ('files', 'head'), None, 100)['rule'] == 'no-activity'
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(120, ('files', 'head'), 119, 100)['rule'] == 'no-first-edit'
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(1, ('changed', 'head'), None, 1) is None
    assert w.decide(62, ('changed', 'head'), None, 1)['rule'] == 'no-progress'
    assert w.decide(180, ('changed', 'head'), 180, 200)['rule'] == 'max'


def test_windows_cpu_fake():
    class Api:
        def cpu_seconds(self, handle):
            assert handle == 42
            return 3.5
    class Runner:
        _win_jobs = {'j': 42}
        win_job_api = Api()
    assert asyncio.run(cpu_seconds(Runner(), 'j', 1, 0)) == 3.5


def test_windows_accounting_layout():
    import ctypes
    from plangraph.spawner.winjob import WinJobApi
    class Kernel:
        def QueryInformationJobObject(self, handle, kind, pointer, size, returned):
            assert kind == 1 and size == 48
            times = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int64 * 4)).contents
            times[0], times[1] = 20_000_000, 15_000_000
            return 1
    api = WinJobApi.__new__(WinJobApi)
    api._k32 = Kernel()
    assert api.cpu_seconds(42) == 3.5


@pytest.mark.skipif(sys.platform != 'linux', reason='real /proc process tree')
@pytest.mark.parametrize('kind,rule', [('headers', 'no-activity'), ('tools', 'no-first-edit'), ('edit', 'max')])
def test_real_run(tmp_path, monkeypatch, kind, rule):
    from plangraph.spawner.runner import SpawnerRunner, JobInfo
    from plangraph.spawner.run_watchdog import collect, edit_signature
    monkeypatch.setenv('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL', '.1')
    scripts = {
        'headers': "import time,subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True);\nwhile True:\n print('> build · m',flush=True)\n for i in range(2000000): pass",
        'tools': "import time\nwhile True:\n print('\\x1b[0m$ \\x1b[0mgit status',flush=True); time.sleep(.1)",
        'edit': "import time; time.sleep(.2); open('changed','w').write('edit')\nwhile True: pass",
    }
    async def run():
        runner = SpawnerRunner(default_cwd=str(tmp_path))
        job = JobInfo('j', 'running', '', 'm', role='review', cwd=str(tmp_path))
        job._watchdog_limits = thresholds({'stall_min': .01, 'first_edit_min': .025, 'max_min': .04})
        job._watchdog_baseline = edit_signature(tmp_path)
        job._live_log = tmp_path / 'logs' / 'live.log'
        runner.jobs['j'] = job
        snapshots = []
        runner.on_sample = lambda jid: snapshots.append((job.stdout, job.last_activity_ts))
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', scripts[kind], cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        output, _ = await collect(runner, 'j', proc)
        event = next(e for e in job.events if e['event'].startswith('watchdog_'))
        assert event['rule'] == rule
        assert job.exit_code == (124 if kind == 'edit' else 6)
        if kind == 'tools':
            assert time.time() - job.start_time >= 1.5
            assert any(text and activity for text, activity in snapshots)
            assert job._live_log.stat().st_size > 0
            assert job._live_log.stat().st_mode & 0o777 == 0o600
        if kind == 'edit':
            assert event['phase'] == 'post-edit'
            assert time.time() - job.start_time >= 2.4
        if kind == 'headers':
            pid = int(output.splitlines()[0])
            # A killed descendant may remain a zombie until its external reaper runs.
            path = __import__('pathlib').Path(f'/proc/{pid}/stat')
            assert not path.exists() or path.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
    asyncio.run(run())
