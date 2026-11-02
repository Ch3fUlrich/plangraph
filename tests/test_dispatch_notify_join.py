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


def _hook(script):
    return ' '.join(shlex.quote(part) for part in (sys.executable, '-c', script))


def _exit_right_after_refusal(tmp_path, command, timing=None):
    child = (
        "import atexit, sys, time\n"
        # registered before exit_outcome's join, so it runs after it (LIFO) and measures it
        + (f"atexit.register(lambda: open({str(timing)!r}, 'w').write(str(time.monotonic() - mark[0])))\n" if timing else "")
        + "mark = [0.0]\nfrom types import SimpleNamespace\n"
        "from plangraph.routing_support.leg_state import LegStateStore\n"
        "from plangraph.spawner.exit_outcome import notify_leg_refused\n"
        "from plangraph.spawner.notify import FileInbox\n"
        "tmp = sys.argv[1]\n"
        "service = SimpleNamespace(leg_reader=LegStateStore(tmp + '/legs.sqlite'), completion_sink=FileInbox(tmp))\n"
        "notify_leg_refused(service, {'run_id': 'run-notifyexit'}, SimpleNamespace(model='m', harness='qodercli'),\n"
        "                   'harness_limit: Error: credits exhausted')\n"
        "mark[0] = time.monotonic()\n")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')
    env = dict(os.environ, PLANGRAPH_SPAWN_NOTIFY_CMD=command, PYTHONPATH=src)
    subprocess.run([sys.executable, '-c', child, str(tmp_path)], env=env, check=True, timeout=60)


def test_the_call_never_joins_the_hook_and_keeps_no_finished_thread(tmp_path, monkeypatch):
    # H2, deterministic: no join from the calling thread; the finished worker leaves the pending list.
    import threading
    from plangraph.spawner import exit_outcome
    log = tmp_path / 'notify-out.log'
    real_join, joined = threading.Thread.join, []

    def join(self, timeout=None):
        if threading.current_thread() is threading.main_thread():
            joined.append(self)
        return real_join(self, timeout)
    monkeypatch.setattr(threading.Thread, 'join', join)
    _notify(tmp_path, monkeypatch, _hook(f"import sys, time; time.sleep(0.3); open({str(log)!r},'a').write(sys.stdin.read())"))
    assert joined == []
    monkeypatch.setattr(threading.Thread, 'join', real_join)
    exit_outcome._join_pending_hooks(budget=30.0)
    assert [json.loads(line)['event'] for line in log.read_text().splitlines() if line.strip()] == ['LEG_REFUSED']
    assert exit_outcome._PENDING_HOOKS == []


def test_a_process_exiting_right_after_the_refusal_still_delivers_the_hook(tmp_path):
    # F3: the interpreter-exit join keeps the hook line of a process that exits at once.
    log = tmp_path / 'notify-out.log'
    _exit_right_after_refusal(tmp_path, _hook(f"import sys, time; time.sleep(0.3); open({str(log)!r},'a').write(sys.stdin.read())"))
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [(e['event'], e['run_id']) for e in events] == [('LEG_REFUSED', 'run-notifyexit')]


def test_a_hanging_hook_holds_the_exit_for_the_2s_budget_only(tmp_path):
    # F3: the exit join waits the budget for a 30 s hook, then abandons it (measured inside the child).
    timing = tmp_path / 'exit-join-seconds'
    _exit_right_after_refusal(tmp_path, _hook('import time; time.sleep(30)'), timing=timing)
    assert 1.5 <= float(timing.read_text()) <= 4.0
