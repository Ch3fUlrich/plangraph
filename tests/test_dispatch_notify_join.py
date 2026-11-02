"""F3 + H2 (D-3605 a): the LEG_REFUSED command hook never blocks the caller; the process joins it at exit.

A run that exited right after a refusal used to lose the hook line (the daemon
thread was still unscheduled when the process died).  The join now happens at
interpreter exit, bounded to 2 s in total, so the event loop never waits.
"""
import json
import os
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.exit_outcome import notify_leg_refused
from plangraph.spawner.notify import FileInbox


def _service(tmp_path, **extra):
    return SimpleNamespace(leg_reader=LegStateStore(tmp_path / 'legs.sqlite'), **extra)


def _notify(tmp_path, monkeypatch, command):
    monkeypatch.setenv('PLANGRAPH_SPAWN_NOTIFY_CMD', command)
    service = _service(tmp_path, completion_sink=FileInbox(tmp_path))
    notify_leg_refused(service, {'run_id': 'run-notifyjoin'},
                       SimpleNamespace(model='m', harness='qodercli'),
                       'harness_limit: Error: credits exhausted')


def _exit_right_after_refusal(tmp_path, command, probe=None):
    child = (
        "import atexit, json, sys, time\n"
        "seen = {}\n"
        # registered before exit_outcome's join, so it runs right AFTER the join (atexit is LIFO)
        + (f"atexit.register(lambda: open({str(probe)!r}, 'w').write(json.dumps(dict(seen, join_s=time.monotonic() - seen['t0']))))\n" if probe else "")
        + "from types import SimpleNamespace\n"
        "from plangraph.routing_support.leg_state import LegStateStore\n"
        "from plangraph.spawner import exit_outcome\n"
        "from plangraph.spawner.notify import FileInbox\n"
        # registered after it, so it runs right BEFORE the join: the hook must still be pending then
        "atexit.register(lambda: seen.update(t0=time.monotonic(), alive=sum(t.is_alive() for t in exit_outcome._PENDING_HOOKS)))\n"
        "tmp = sys.argv[1]\n"
        "service = SimpleNamespace(leg_reader=LegStateStore(tmp + '/legs.sqlite'), completion_sink=FileInbox(tmp))\n"
        "exit_outcome.notify_leg_refused(service, {'run_id': 'run-notifyexit'}, SimpleNamespace(model='m', harness='qodercli'),\n"
        "                                'harness_limit: Error: credits exhausted')\n")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')
    env = dict(os.environ, PLANGRAPH_SPAWN_NOTIFY_CMD=command, PYTHONPATH=src)
    subprocess.run([sys.executable, '-c', child, str(tmp_path)], env=env, check=True, timeout=60)


def _script_hook(tmp_path, body):
    path = tmp_path / 'hook.py'
    path.write_text(body)
    return _hook_file(path)


def _hook_file(path):
    return ' '.join(shlex.quote(part) for part in (sys.executable, str(path)))


def test_the_call_returns_while_the_hook_is_still_running(tmp_path, monkeypatch):
    # H2, deterministic: the hook waits on a gate the test opens only after the call returned, and the calling
    # thread never joins; then the exit join delivers it and no finished thread is kept.
    import threading
    from plangraph.spawner import exit_outcome
    log, gate = tmp_path / 'notify-out.log', tmp_path / 'gate'
    real_join, joined = threading.Thread.join, []

    def join(self, timeout=None):
        if threading.current_thread() is threading.main_thread():
            joined.append(self)
        return real_join(self, timeout)
    monkeypatch.setattr(threading.Thread, 'join', join)
    hook = _script_hook(tmp_path, f"import os, sys, time\nwhile not os.path.exists({str(gate)!r}):\n    time.sleep(0.05)\n"
                                  f"open({str(log)!r}, 'a').write(sys.stdin.read())\n")
    _notify(tmp_path, monkeypatch, hook)
    assert joined == [] and not log.exists()
    assert [t.is_alive() for t in exit_outcome._PENDING_HOOKS] == [True]   # returned while the hook still runs
    gate.write_text('')
    monkeypatch.setattr(threading.Thread, 'join', real_join)
    exit_outcome._join_pending_hooks(budget=30.0)
    assert [json.loads(line)['event'] for line in log.read_text().splitlines() if line.strip()] == ['LEG_REFUSED']
    assert exit_outcome._PENDING_HOOKS == []


def test_a_process_exiting_right_after_the_refusal_still_delivers_the_hook(tmp_path):
    # F3: the interpreter-exit join keeps the hook line of a process that exits at once.
    log = tmp_path / 'notify-out.log'
    _exit_right_after_refusal(tmp_path, _script_hook(tmp_path, f"import sys, time\ntime.sleep(0.3)\nopen({str(log)!r}, 'a').write(sys.stdin.read())\n"))
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [(e['event'], e['run_id']) for e in events] == [('LEG_REFUSED', 'run-notifyexit')]


def test_the_exit_join_runs_for_a_pending_hook_and_abandons_it_at_the_budget(tmp_path):
    # F3, instrumented in the child: the hook is still pending when the exit join starts, and the join waits the
    # 2 s budget, never the hook's 30 s (lower bound proves the join ran; the upper bound is CI-tolerant).
    probe = tmp_path / 'exit-join.json'
    _exit_right_after_refusal(tmp_path, _script_hook(tmp_path, "import time\ntime.sleep(30)\n"), probe=probe)
    seen = json.loads(probe.read_text())
    assert seen['alive'] == 1
    assert 1.5 <= seen['join_s'] <= 10.0
