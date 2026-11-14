"""Run watchdog decisions and platform sampling contracts."""
import math
import asyncio
import os
from pathlib import Path
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


def test_bad_env_timeout_refused(monkeypatch):
    # M17: a bad env knob is a refusal (exit 2 path), not a generic failure.
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', 'x')
    with pytest.raises(SpawnRefused):
        thresholds()


def test_bad_env_timeout_refused_before_launch(tmp_path, monkeypatch):
    from plangraph.spawner.runner import spawn_service
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', 'x')
    class Runner:
        default_cwd = str(tmp_path)
        jobs = {}
        async def spawn(self, **kwargs):
            pytest.fail('invalid timeout launched a child')
    async def run():
        service = spawn_service(runner=Runner(), state=tmp_path)
        service._route = lambda *a, **kw: SpawnPlan((DEFAULT_LEGS[0],), (), 'test', (), ())
        record = await service.spawn({'estimate_tokens': 1000}, wait_seconds=None)
        assert record['status'] == 'refused'
        assert record['exit_code'] == 2
    asyncio.run(run())


def test_cpu_ignored_before_edit():
    w = Watchdog(thresholds({'stall_min': 1}), 0, ('files', 'head'))
    assert w.decide(1, ('files', 'head'), 1, 1) is None
    assert w.decide(61, ('files', 'head'), 1, 900)['rule'] == 'no-activity'


def test_buffered_writer_waits_for_first_edit():
    # Q6: a harness that buffers output until exit gives no activity signal;
    # 'no-activity' must not kill a working writer before first_edit_min.
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 90})
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(61, ('files', 'head'), None, None) is None
    assert w.decide(121, ('files', 'head'), None, None)['rule'] == 'no-first-edit'


def test_no_first_edit_holds_while_output_flows():
    # M5: no-first-edit means no output AND no edit; flowing tool output is work.
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 3})
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(121, ('files', 'head'), 120, None) is None
    assert w.decide(200, ('files', 'head'), 199, None) is None
    assert w.decide(300, ('files', 'head'), 299, None)['rule'] == 'max'


def test_unavailable_baseline_adopts_first_signature():
    # Q7/M4: a None spawn baseline must not pin the run in pre-edit forever.
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 3})
    w = Watchdog(limits, 0, None)
    assert w.decide(1, None, 30, None) is None
    assert w.decide(30, ('files', 'head'), 30, None) is None
    assert not w.edited
    assert w.decide(121, ('changed', 'head'), 30, None) is None
    assert w.edited


def test_edit_signal_unavailable_never_kills_before_max():
    # Q7/M4: while edits cannot be observed, no kill rule may assert 'no edit'.
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 3})
    w = Watchdog(limits, 0, None)
    assert w.decide(121, None, 1, 1) is None
    assert w.decide(180, None, 1, 1)['rule'] == 'max'


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
    assert w.decide(61, ('files', 'head'), 1, 100)['rule'] == 'no-activity'
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(60, ('files', 'head'), None, 100) is None
    assert w.decide(121, ('files', 'head'), None, 100)['rule'] == 'no-first-edit'
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(121, ('files', 'head'), 120, 100) is None
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


def _own(runner, proc, job_id='j'):
    from plangraph.spawner.proc_kill import root_identity   # #336 fail-closed reap: register the real root as ours
    runner._owned = {**getattr(runner, '_owned', {}), job_id: dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}


def _job(tmp_path, limits):
    from plangraph.spawner.runner import SpawnerRunner, JobInfo
    from plangraph.spawner.run_watchdog import edit_signature
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo('j', 'running', '', 'm', role='implement', cwd=str(tmp_path))
    job._watchdog_limits = thresholds(limits)
    job._watchdog_baseline = edit_signature(tmp_path)
    job._live_log = tmp_path / 'logs' / 'live.log'
    runner.jobs['j'] = job
    return runner, job


def test_output_scrubbed_once_and_live_log_batched(tmp_path, monkeypatch):
    # Q4/M3: scrub each new chunk once (linear), keep a bounded tail and batch
    # live.log appends; never re-scrub accumulated output per read or per line.
    from plangraph.spawner import runner as runner_mod, private_io, env_redact
    from plangraph.spawner.run_watchdog import collect
    real_append = private_io.append_private
    scrubbed_len = 0
    def count_scrub(value):
        nonlocal scrubbed_len
        scrubbed_len += len(value)
        return value
    monkeypatch.setattr(runner_mod, 'redact', count_scrub)
    monkeypatch.setattr(env_redact, 'redact_env_values', lambda value, env: count_scrub(value))
    appends = []
    def count_append(path, text):
        appends.append(len(text))
        return real_append(path, text)
    monkeypatch.setattr(private_io, 'append_private', count_append)
    script = ("import sys\n"
              "line = 'x' * 1023 + '\\n'\n"
              "for _ in range(2000):\n"
              "    sys.stdout.write(line)\n")
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        output, _ = await collect(runner, 'j', proc)
        total = len(output)
        assert output.endswith(b'x' * 1023 + os.linesep.encode())
        assert len(job.stdout) <= 4096                     # bounded tail
        assert scrubbed_len <= 3 * total                   # linear, not O(n^2)
        assert len(appends) <= 2 * (total // 16384) + 4    # batched, not per line
        assert sum(appends) >= total                       # every byte persisted
        assert job._live_log.stat().st_size >= total
        assert not [e for e in job.events if e['event'] == 'live_log_error']
    asyncio.run(run())


def test_live_log_write_error_is_an_event_not_a_failure(tmp_path, monkeypatch):
    # Q5: a live.log write error must never fail a run whose child finished fine.
    from plangraph.spawner import private_io
    from plangraph.spawner.run_watchdog import collect
    def broken(path, text):
        raise OSError('ENOSPC')
    monkeypatch.setattr(private_io, 'append_private', broken)
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', "print('hello')", cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        output, _ = await collect(runner, 'j', proc)   # must not raise
        assert output.strip() == b'hello'
        errors = [e for e in job.events if e['event'] == 'live_log_error']
        assert errors and errors[0]['error'] == 'OSError'
        assert len(errors) <= 3
        assert job.status == 'running'
    asyncio.run(run())


def test_live_log_rotate_error_is_an_event_not_a_failure(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    from plangraph.spawner import run_watchdog
    from plangraph.spawner.run_watchdog import collect
    monkeypatch.setattr(run_watchdog, 'LIVE_LOG_MAX', 2048)
    def broken(path, text):
        raise OSError('sharing violation')
    monkeypatch.setattr(private_io, 'atomic_write_private', broken)
    script = "import sys; sys.stdout.write('y' * 8192)"
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        output, _ = await collect(runner, 'j', proc)   # must not raise
        assert output == b'y' * 8192
        assert [e for e in job.events if e['event'] == 'live_log_error']
        assert job.status == 'running'
    asyncio.run(run())


@pytest.mark.skipif(sys.platform != 'linux', reason='process groups and /proc')
def test_detached_grandchild_bounds_drain(tmp_path, monkeypatch):
    # M6: a detached grandchild holding the pipes must not keep collect (and the
    # run record) pending forever; readers are cancelled after a short grace.
    import signal
    from plangraph.spawner import run_watchdog
    from plangraph.spawner.run_watchdog import collect
    monkeypatch.setenv('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL', '.1')
    monkeypatch.setattr(run_watchdog, 'READER_GRACE_S', .5)
    script = ("import subprocess,sys\n"
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
              "                     start_new_session=True)\n"
              "open('gp', 'w').write(str(p.pid))\n"
              "print(p.pid, flush=True)\n"
              "import time; time.sleep(.2)\n")
    async def run():
        runner, job = _job(tmp_path, {'stall_min': .05, 'first_edit_min': .1, 'max_min': .2})
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        started = time.time()
        try:
            output, _ = await asyncio.wait_for(collect(runner, 'j', proc), timeout=10)
        finally:
            try:
                os.killpg(int((tmp_path / 'gp').read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        assert time.time() - started < 6    # bounded by exit + grace, not the 60 s grandchild
        assert output.splitlines()[0].isdigit()
    asyncio.run(run())


@pytest.mark.skipif(sys.platform != 'linux', reason='real /proc process tree')
@pytest.mark.parametrize('kind,rule', [('headers', 'no-activity'), ('tools', 'max'), ('edit', 'no-progress')])
def test_real_run(tmp_path, monkeypatch, kind, rule):
    from plangraph.spawner.runner import SpawnerRunner, JobInfo
    from plangraph.spawner.run_watchdog import collect, edit_signature
    monkeypatch.setenv('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL', '.1')
    # r3 NIT: a fake clock (frozen through child start-up, then stepped in 0.25 s
    # quanta) keeps the rules and the timing asserts independent of machine load.
    import math
    import types
    import plangraph.spawner.run_watchdog as run_watchdog
    real_time = time.time
    epoch = real_time()
    fake_time = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith('_')})
    fake_time.time = lambda: epoch + max(0.0, math.ceil((real_time() - epoch - 2.0) / .25) * .25)
    monkeypatch.setattr(run_watchdog, 'time', fake_time)        # this module only, not the stdlib
    scripts = {
        'headers': ("import subprocess,sys,time\n"
                    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                    "print(p.pid, flush=True)\n"
                    "while True:\n"
                    "    print('> build · m', flush=True)\n"
                    "    time.sleep(.05)\n"),
        'tools': ("import time\n"
                  "while True:\n"
                  "    print('\\x1b[0m$ \\x1b[0mgit status', flush=True)\n"
                  "    time.sleep(.1)\n"),
        'edit': ("import time\n"
                 "time.sleep(.2)\n"
                 "open('changed', 'w').write('edit')\n"
                 "while True:\n"
                 "    time.sleep(60)\n"),
    }
    async def run():
        runner, job = _job(tmp_path, {'stall_min': .005, 'first_edit_min': .015, 'max_min': .035})
        snapshots = []
        runner.on_sample = lambda jid: snapshots.append((job.stdout, job.last_activity_ts))
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', scripts[kind], cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        from plangraph.spawner.proc_kill import root_identity
        # r3 (V4): the real root is ours; register it so reap proves ownership instead of kill_incomplete
        runner._owned = {'j': dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        output, _ = await collect(runner, 'j', proc)
        event = next(e for e in job.events if e['event'].startswith('watchdog_'))
        assert event['rule'] == rule
        assert job.exit_code == (124 if rule == 'max' else 6)
        if kind == 'tools':
            assert event['phase'] == 'pre-edit'
            assert time.time() - job.start_time >= 0.9      # flowed past first_edit_min
            assert any(text and activity for text, activity in snapshots)
            assert job._live_log.stat().st_size > 0
            assert job._live_log.stat().st_mode & 0o777 == 0o600
        if kind == 'edit':
            assert event['phase'] == 'post-edit'
            assert time.time() - job.start_time >= 0.5
        if kind == 'headers':
            pid = int(output.splitlines()[0])
            # A killed descendant may take a moment to die (CI saw state R right
            # after collect) or linger as a zombie until its external reaper runs.
            path = Path(f'/proc/{pid}/stat')
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    stat = path.read_text()
                except OSError:
                    break
                if stat.rsplit(')', 1)[1].split()[0] == 'Z':
                    break
                time.sleep(.05)
            assert not path.exists() or path.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
    asyncio.run(run())


def test_review_seat_only_max_rule():
    # Seats never edit and qodercli -p buffers output: quiet 25 min is a working
    # review (run-a0217dad, run-feca26fc were killed as stalled).
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 90})
    w = Watchdog(limits, 0, ('files', 'head'), writer=False)
    assert w.decide(1500, ('files', 'head'), None, None) is None
    assert w.decide(90 * 60, ('files', 'head'), None, None)['rule'] == 'max'


def test_d1_role_caps_are_hard_walls(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    implement = thresholds(role='implement')
    review = thresholds(role='review')
    assert (implement['stall_s'], implement['max_s']) == (1500, 2700)
    assert review['max_s'] == 600
    assert Watchdog(implement, 0, 'before').budget() == 2700
    assert Watchdog(review, 0, 'before').budget() == 600


def test_d1_measured_card_class_widens_defaults(tmp_path, monkeypatch):
    import json
    path = tmp_path / 'p90.json'
    path.write_text(json.dumps({'large': {'implement': {'stall_min': 30, 'max_min': 55}}}))
    monkeypatch.setenv('PLANGRAPH_SPAWN_P90_TABLE', str(path))
    limits = thresholds({'card_class': 'large'}, role='implement')
    assert (limits['stall_s'], limits['max_s']) == (1800, 3300)
    assert limits['hard_max'] is True
    assert thresholds({'card_class': 'large', 'max_min': 2}, role='implement')['max_s'] == 120


@pytest.mark.parametrize('cpu', [0, 1, None])
def test_d1_stuck_requires_silent_stdout_and_proven_zero_cpu_delta(cpu):
    w = Watchdog(thresholds({'stuck_min': 3}, role='review'), 0, None, writer=False)
    assert w.decide(0, None, None, 0, stdout_activity=0) is None
    event = w.decide(180, None, 179, cpu, stdout_activity=0)
    if cpu == 0:
        assert event['rule'] == 'stuck'
    else:
        assert event is None


def test_d1_tool_loop_deduplicates_updates_and_hashes_arguments():
    import json
    from plangraph.spawner.stall_probe import ToolLoop
    gate = ToolLoop()
    def call(call_id, args, name='bash'):
        return json.dumps({'type': 'tool_use', 'part': {'callID': call_id, 'tool': name,
            'state': {'status': 'running', 'input': args}}})
    assert not gate.feed(call('a', {'x': 1, 'y': 2}))
    assert not gate.feed(call('a', {'y': 2, 'x': 1}))
    assert not gate.feed(call('b', {'y': 2, 'x': 1}))
    assert gate.feed(call('c', {'x': 1, 'y': 2}))
    other = ToolLoop()
    assert not other.feed(call('a', {'x': 1}))
    assert not other.feed(call('b', {'x': 2}))
    assert not other.feed(call('c', {'x': 1}))
    assert not other.feed(call('d', {'x': 1}, 'read'))


@pytest.mark.parametrize('transport', ['tool_call', 'opencode', 'claude', 'codex'])
def test_d1_tool_loop_reaps_fake_harness_within_five_seconds(tmp_path, monkeypatch, transport):
    import json
    from types import SimpleNamespace
    from plangraph.spawner import run_watchdog as monitor
    async def run():
        runner, job = _job(tmp_path, {'max_min': 45})
        proc = SimpleNamespace(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(),
                               pid=None, returncode=None)
        for n in range(3):
            call = dict(type='tool_call', id=str(n), name='read', arguments={'path': 'same'})
            if transport == 'opencode':
                call = dict(type='tool_use', part=dict(callID=str(n), tool='read', state=dict(input={'path': 'same'})))
            elif transport == 'claude':
                call = dict(type='assistant', message=dict(content=[dict(type='tool_use', id=str(n), name='read', input={'path': 'same'})]))
            elif transport == 'codex':
                call = dict(type='item.completed', item=call)
            proc.stdout.feed_data((json.dumps(call) + '\n').encode())
        async def reap(*args):
            proc.returncode = -15
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
        monkeypatch.setattr(runner, '_reap_process', reap)
        started = time.monotonic()
        await asyncio.wait_for(monitor.collect(runner, 'j', proc), 5)
        assert time.monotonic() - started < 5
        record = {'role': 'implement'}
        assert monitor.terminal_record(record, job)
        assert record['reason'] == 'tool_loop'
        assert job.status == 'stalled'
    asyncio.run(run())


def test_d1_implement_fallthroughs_share_one_hard_deadline(monkeypatch):
    from plangraph.spawner import run_watchdog_limits as limits
    monkeypatch.setattr(limits.time, 'time', lambda: 100)
    cap = thresholds(role='implement')
    record = {'role': 'implement', 'started': 0}
    assert limits.leg_limits(record, cap)['deadline'] == 2700
    monkeypatch.setattr(limits.time, 'time', lambda: 2700)
    assert limits.leg_limits(record, cap) is None
    assert record['reason'] == 'timeout'


@pytest.mark.parametrize('row', [{'max_min': 0}, {'max_min': True}, {'max_min': '55'}, {'other': 20}])
def test_d1_bad_p90_rows_refused(tmp_path, monkeypatch, row):
    import json
    path = tmp_path / 'p90.json'
    path.write_text(json.dumps({'large': {'implement': row}}))
    monkeypatch.setenv('PLANGRAPH_SPAWN_P90_TABLE', str(path))
    with pytest.raises(SpawnRefused, match='p90'):
        thresholds({'card_class': 'large'}, role='implement')


def test_d1_cancelled_cpu_sample_does_not_cancel_other_monitors(monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import run_cpu
    async def run():
        release = asyncio.Event()
        async def scan(*args):
            await release.wait()
            return {}
        monkeypatch.setattr(run_cpu.asyncio, 'to_thread', scan)
        runner = SimpleNamespace()
        one = asyncio.create_task(run_cpu.cpu_seconds(runner, 'one', 123, 0))
        two = asyncio.create_task(run_cpu.cpu_seconds(runner, 'two', 124, 0))
        await asyncio.sleep(.01)
        one.cancel()
        await asyncio.gather(one, return_exceptions=True)
        release.set()
        assert await two == 0
    asyncio.run(run())
