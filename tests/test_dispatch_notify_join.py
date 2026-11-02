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


def _exit_right_after_refusal(tmp_path, command):
    child = (
        "import sys\nfrom types import SimpleNamespace\n"
        "from plangraph.routing_support.leg_state import LegStateStore\n"
        "from plangraph.spawner.exit_outcome import notify_leg_refused\n"
        "from plangraph.spawner.notify import FileInbox\n"
        "tmp = sys.argv[1]\n"
        "service = SimpleNamespace(leg_reader=LegStateStore(tmp + '/legs.sqlite'), completion_sink=FileInbox(tmp))\n"
        "notify_leg_refused(service, {'run_id': 'run-notifyexit'}, SimpleNamespace(model='m', harness='qodercli'),\n"
        "                   'harness_limit: Error: credits exhausted')\n")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')
    env = dict(os.environ, PLANGRAPH_SPAWN_NOTIFY_CMD=command, PYTHONPATH=src)
    before = time.time()
    subprocess.run([sys.executable, '-c', child, str(tmp_path)], env=env, check=True, timeout=30)
    return time.time() - before


def test_the_call_never_waits_for_the_hook(tmp_path, monkeypatch):
    # H2: a 1 s hook does not hold the caller (the event loop); the exit-time join then delivers it.
    from plangraph.spawner.exit_outcome import _join_pending_hooks
    log = tmp_path / 'notify-out.log'
    before = time.time()
    _notify(tmp_path, monkeypatch, _hook(f"import sys, time; time.sleep(1.0); open({str(log)!r},'a').write(sys.stdin.read())"))
    assert time.time() - before < 0.5
    _join_pending_hooks()
    assert [json.loads(line)['event'] for line in log.read_text().splitlines() if line.strip()] == ['LEG_REFUSED']


def test_a_process_exiting_right_after_the_refusal_still_delivers_the_hook(tmp_path):
    # F3: the interpreter-exit join keeps the hook line of a process that exits at once.
    log = tmp_path / 'notify-out.log'
    _exit_right_after_refusal(tmp_path, _hook(f"import sys, time; time.sleep(0.3); open({str(log)!r},'a').write(sys.stdin.read())"))
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [(e['event'], e['run_id']) for e in events] == [('LEG_REFUSED', 'run-notifyexit')]


def test_a_hanging_hook_delays_the_exit_by_at_most_two_seconds(tmp_path):
    # F3: a 30 s hook is abandoned after the 2 s exit budget.
    assert _exit_right_after_refusal(tmp_path, _hook('import time; time.sleep(30)')) < 10
