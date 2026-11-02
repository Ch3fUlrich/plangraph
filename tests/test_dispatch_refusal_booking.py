"""Review G1/G2/G3 (D-3605 a): validated leg-down time on every harness-limit
line, and exactly one LEG_REFUSED notify per refused attempt."""
import json
import shlex
import sys
import time
from types import SimpleNamespace

import pytest

from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.exit_outcome import fallthrough_reason
from plangraph.spawner.notify import FileInbox

CREDIT = 'Error: credits exhausted'
NO_RESET = 'Error: rate limit exceeded, retry later'
WITH_RESET = 'error: Individual quota reached. Resets in 10h40m26s.'


def _service(tmp_path, **extra):
    return SimpleNamespace(leg_reader=LegStateStore(tmp_path / 'legs.sqlite'), **extra)


def _job(line, exit_code=7):
    return SimpleNamespace(exit_code=exit_code, stdout='', stderr=line)


@pytest.mark.parametrize('value', ['abc', '-5', 'inf', 'nan'])
def test_invalid_leg_down_s_falls_back_and_books_credit_refusal(tmp_path, monkeypatch, value):
    monkeypatch.setenv('PLANGRAPH_LEG_DOWN_S', value)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    before = time.time()
    reason = fallthrough_reason(_service(tmp_path), SimpleNamespace(model='m', harness='qodercli'),
                                _job(CREDIT), {})
    after = time.time()
    assert reason.startswith('harness_limit:')
    reset_at = store.leg_status('m')['evidence']['reset_at']
    assert before + 1800 <= reset_at <= after + 1800     # fallback 1800 s, never a raise


def test_no_reset_line_books_down_window_reset_line_keeps_its_own(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_LEG_DOWN_S', raising=False)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    service = _service(tmp_path)
    before = time.time()
    fallthrough_reason(service, SimpleNamespace(model='m', harness='qodercli'), _job(NO_RESET), {})
    after = time.time()
    reset_at = store.leg_status('m')['evidence']['reset_at']
    assert before + 1800 <= reset_at <= after + 1800     # unavailable_until, not a 10-minute hint
    before = time.time()
    fallthrough_reason(service, SimpleNamespace(model='other', harness='qodercli'), _job(WITH_RESET), {})
    after = time.time()
    reset_at = store.leg_status('other')['evidence']['reset_at']
    assert before + 38426 <= reset_at <= after + 38426   # 'Resets in 10h40m26s' is unchanged


@pytest.mark.parametrize('value, window', [('0', 60.0), ('1e-9', 60.0),
                                           ('1e308', 86400.0), (None, 1800.0)])
def test_booking_time_clamped_to_minute_and_day(tmp_path, monkeypatch, value, window):
    # H1: '0'/'1e-9' would never bring the leg back; '1e308' is near-permanent.
    if value is None:
        monkeypatch.delenv('PLANGRAPH_LEG_DOWN_S', raising=False)   # default 1800
    else:
        monkeypatch.setenv('PLANGRAPH_LEG_DOWN_S', value)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    before = time.time()
    fallthrough_reason(_service(tmp_path), SimpleNamespace(model='m', harness='qodercli'),
                        _job(CREDIT), {})
    after = time.time()
    reset_at = store.leg_status('m')['evidence']['reset_at']
    assert before + window - 5 <= reset_at <= after + window + 5     # 5 s tolerance


def test_notify_cmd_delivers_leg_refused_line_off_the_event_loop(tmp_path, monkeypatch):
    # H2: the command hook runs on a daemon thread; its stdin is the event JSON.
    log = tmp_path / 'notify-stdin.log'
    script = f"import sys; open({str(log)!r},'a').write(sys.stdin.read())"
    command = ' '.join(shlex.quote(part) for part in (sys.executable, '-c', script))
    monkeypatch.setenv('PLANGRAPH_SPAWN_NOTIFY_CMD', command)   # sink appends 'parent' argv
    service = _service(tmp_path, completion_sink=FileInbox(tmp_path))
    record = {'run_id': 'run-booking3'}
    fallthrough_reason(service, SimpleNamespace(model='m', harness='qodercli'), _job(CREDIT), record)
    events, deadline = [], time.time() + 5
    while time.time() < deadline:
        if log.exists():
            events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
            if events:
                break
        time.sleep(0.05)
    refused = [e for e in events if e['event'] == 'LEG_REFUSED']
    assert len(refused) == 1
    assert refused[0]['run_id'] == 'run-booking3'
    assert refused[0]['leg'] == 'm'
    assert refused[0]['reason'].startswith('harness_limit:')


def test_notify_cmd_missing_binary_never_raises_out_of_fallthrough(tmp_path, monkeypatch):
    # H2: a failing command hook is logged on its daemon thread, never raised here.
    monkeypatch.setenv('PLANGRAPH_SPAWN_NOTIFY_CMD', str(tmp_path / 'no-such-notify-cmd'))
    service = _service(tmp_path, completion_sink=FileInbox(tmp_path))
    reason = fallthrough_reason(service, SimpleNamespace(model='m', harness='qodercli'),
                                _job(CREDIT), {'run_id': 'run-booking4'})
    assert reason.startswith('harness_limit:')


def test_notify_leg_refused_delivers_one_line_per_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    service = _service(tmp_path, completion_sink=FileInbox(tmp_path))
    record = {'run_id': 'run-booking1'}
    chosen = SimpleNamespace(model='m', harness='qodercli')
    fallthrough_reason(service, chosen, _job(CREDIT), record)
    fallthrough_reason(service, chosen, _job(CREDIT), record)   # FileInbox dedupes on idempotency_key
    lines = [json.loads(line) for line in (tmp_path / 'inbox' / 'parent.jsonl').read_text().splitlines()]
    refused = [e for e in lines if e['event'] == 'LEG_REFUSED']
    assert len(refused) == 1
    assert refused[0]['run_id'] == 'run-booking1'
    assert refused[0]['leg'] == 'm'
    assert refused[0]['reason'].startswith('harness_limit:')


def test_service_without_completion_sink_stays_silent(tmp_path):
    reason = fallthrough_reason(_service(tmp_path), SimpleNamespace(model='m', harness='qodercli'),
                                _job(CREDIT), {'run_id': 'run-booking2'})
    assert reason.startswith('harness_limit:')
    assert not (tmp_path / 'inbox' / 'parent.jsonl').exists()
