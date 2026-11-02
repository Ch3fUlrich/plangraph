"""F3 (D-3605 a): the LEG_REFUSED command hook is joined for up to 2 s.

A run that exited right after a refusal used to lose the hook line: the daemon
thread was still unscheduled when the process died.  notify_leg_refused now
joins the delivery thread with a bounded timeout -- a quick hook has delivered
before the call returns, and a hanging hook never blocks the caller longer
than ~2.5 s.
"""
import json
import shlex
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


def test_notify_hook_delivered_before_the_call_returns(tmp_path, monkeypatch):
    # F3: a 0.3 s hook appends its event to the tmp file before the call returns.
    log = tmp_path / 'notify-out.log'
    script = f"import sys, time; time.sleep(0.3); open({str(log)!r},'a').write(sys.stdin.read())"
    command = ' '.join(shlex.quote(part) for part in (sys.executable, '-c', script))
    _notify(tmp_path, monkeypatch, command)
    assert log.exists()                       # written by the time the call returned
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [e['event'] for e in events] == ['LEG_REFUSED']
    assert events[0]['run_id'] == 'run-notifyjoin'


def test_hanging_notify_hook_never_blocks_the_caller(tmp_path, monkeypatch):
    # F3: the join is bounded -- a 30 s hook is abandoned, the call returns < 3.5 s.
    script = 'import time; time.sleep(30)'
    command = ' '.join(shlex.quote(part) for part in (sys.executable, '-c', script))
    before = time.time()
    _notify(tmp_path, monkeypatch, command)
    assert time.time() - before < 3.5
