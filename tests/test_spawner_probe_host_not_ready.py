"""Host executable readiness must never become shared leg outage evidence."""
import asyncio
import shutil
import sqlite3
from types import SimpleNamespace

from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner import leg_probe


def leg(harness, model='sample/model'):
    return SimpleNamespace(id=harness + '-sample', harness=harness, model=model,
                           roles=('implement',))


class FailingRunner:
    def __init__(self, store):
        self.leg_reader = store
        self.attempts = []

    async def spawn(self, **kwargs):
        self.attempts.append(kwargs['client'])
        return SimpleNamespace(job_id='failed', status='failed', exit_code=1)

    async def cancel(self, job_id):
        pass


def events(store):
    if not store.path.exists():
        return 0
    with sqlite3.connect(store.path) as db:
        return db.execute('SELECT count(*) FROM leg_events').fetchone()[0]


def test_absent_binary_is_host_not_ready_without_attempt_or_evidence(tmp_path, monkeypatch):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = FailingRunner(store)
    monkeypatch.setattr(shutil, 'which', lambda *a, **kw: None)
    # Any repository setup would already be too late for this host check.
    monkeypatch.setattr(leg_probe.tempfile, 'mkdtemp',
                        lambda **kw: (_ for _ in ()).throw(AssertionError('leg attempted')))
    for _ in range(2):
        result = asyncio.run(leg_probe.probe(runner, leg('agy'), store=store))
        assert result['state'] == 'host-not-ready'
        assert result['infra'] and not result['ok']
    assert runner.attempts == []
    assert store.leg_status('sample/model')['state'] == 'ok'
    assert events(store) == 0


def test_present_failing_binary_still_books_down(tmp_path, monkeypatch):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = FailingRunner(store)
    monkeypatch.setattr(shutil, 'which', lambda *a, **kw: '/synthetic/harness')
    for _ in range(2):
        result = asyncio.run(leg_probe.probe(runner, leg('agy'), store=store))
        assert result['reason'] == 'exit 1'
        assert not result['infra']
    assert runner.attempts == ['agy', 'agy']
    assert store.leg_status('sample/model')['state'] == 'down'
    assert events(store) == 2


def test_partial_host_only_marks_missing_harness(tmp_path, monkeypatch):
    from plangraph.planner import legs
    from plangraph.routing_support.liveness import unavailable

    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = FailingRunner(store)
    present, absent = leg('agy'), leg('qodercli', 'sample/other')
    monkeypatch.setattr(shutil, 'which',
                        lambda name, **kw: '/synthetic/harness' if name == 'agy' else None)
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (present, absent))
    monkeypatch.setattr(legs, 'leg_active', lambda row: True)
    for _ in range(2):
        result = asyncio.run(leg_probe.probe_many(runner, all_writers=True))
    rows = result['probes']
    assert rows[0]['reason'] == 'exit 1'
    assert rows[1]['state'] == 'host-not-ready'
    assert 'host-not-ready' in rows[1]['reason']  # CLI text uses reason; JSON uses state.
    assert runner.attempts == ['agy', 'agy']
    assert store.leg_status(present.model)['state'] == 'down'
    # The route table reads leg status, not a host-local probe verdict.
    assert not unavailable(store.leg_status(absent.model))
    assert store.leg_status(absent.model)['state'] == 'ok'
    assert events(store) == 2
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    reason = asyncio.run(leg_probe.before_launch(runner, absent, 'implement', False))
    assert reason.startswith('host-not-ready:')
    assert events(store) == 2
