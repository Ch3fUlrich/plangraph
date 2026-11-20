"""not_served_on_host is a host fact (a unit env that lacks the opencode config), never a provider 429."""
import sqlite3
from types import SimpleNamespace

import pytest

from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.exit_outcome import fallthrough_reason

MODEL = 'console/deepseek-flash'


def _run(tmp_path, stderr):
    service = SimpleNamespace(leg_reader=LegStateStore(tmp_path / 'legs.sqlite'))
    job = SimpleNamespace(exit_code=1, stdout='', stderr=stderr)
    reason = fallthrough_reason(service, SimpleNamespace(model=MODEL, harness='opencode'), job, {})
    with sqlite3.connect(tmp_path / 'legs.sqlite') as db:
        events = db.execute('SELECT status FROM leg_events').fetchall() if _has_events(db) else []
    return reason, events, service.leg_reader.leg_status(MODEL)


def _has_events(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='leg_events'").fetchone())


def test_not_served_on_host_is_reported_but_not_booked_as_a_429(tmp_path):
    reason, events, status = _run(tmp_path, f'not_served_on_host: {MODEL}')
    assert reason == f'not_served_on_host: {MODEL}'
    assert events == [] and status['state'] == 'ok'


@pytest.mark.parametrize('stderr', ['Error: unknown model', 'Error: model unavailable'])
def test_a_provider_side_unavailable_model_is_still_booked(tmp_path, stderr):
    reason, events, status = _run(tmp_path, stderr)
    assert reason and events == [(429,)] and status['state'] != 'ok'
