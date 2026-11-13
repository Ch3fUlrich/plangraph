"""Ownerless bounded queues must stop suppressing watchdog findings durably."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner import stall_watchdog as w
from conftest_watchdog import isolated_process_evidence  # noqa: F401


@pytest.fixture
def queued(tmp_path, monkeypatch):
    monkeypatch.setattr(w, 'time', SimpleNamespace(time=lambda: 10000))
    def write(name='expired', **fields):
        path = tmp_path / f'runs/run-{name}/record.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(run_id=f'run-{name}', status='queued', started=1000,
                      ended=None, exit_code=0, reason='memory_recovery',
                      parent_session='parent', item_key='plangraph:QUEUE')
        record.update(fields)
        path.write_text(json.dumps(record))
        return path, record
    return write


@pytest.mark.parametrize('fields', [dict(queued_until=9900), dict(queued_until=10000),
                                  dict(status='queue_claimed', queued_until=9900),
                                  dict(queue_bound_s=30), {}, dict(started=0)])
def test_ownerless_queue_timeout_is_persisted(tmp_path, queued, fields):
    path, original = queued(**fields)
    [record] = w.read_runs(tmp_path)
    assert record['status'] == 'failed'
    assert record == json.loads(path.read_text()) == dict(
        original, status='failed', reason='queue_timeout', error='queue_timeout',
        ended=10000, exit_code=1)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('fields', [dict(queued_until=10001), dict(queue_bound_s=9001)])
def test_queue_sweep_leaves_bounded_record_untouched(tmp_path, queued, fields):
    queued(queued_until=9900)
    path, original = queued('bounded', **fields)
    before = path.read_bytes(), path.stat().st_mtime_ns
    records = {r['run_id']: r for r in w.read_runs(tmp_path)}
    assert records['run-expired']['reason'] == 'queue_timeout'
    assert records['run-bounded'] == original
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_queue_sweep_is_idempotent(tmp_path, queued, monkeypatch):
    from plangraph.spawner import private_io
    path, _ = queued(queued_until=9900)
    first = w.read_runs(tmp_path)
    assert first[0]['reason'] == 'queue_timeout'
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(private_io, 'atomic_write_private',
                        lambda *a, **kw: pytest.fail('terminal queue rewritten'))
    monkeypatch.setattr(w, 'time', SimpleNamespace(time=lambda: 20000))
    assert w.read_runs(tmp_path) == first
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_queue_sweep_dry_run_projects_timeout_without_writing(tmp_path, queued):
    path, original = queued(queued_until=9900)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    [record] = w.read_runs(tmp_path, dry_run=True)
    assert record['reason'] == 'queue_timeout'
    assert json.loads(path.read_text()) == original
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('fields', [dict(queued_until='bad'), dict(queued_until=True),
                                  dict(queued_until=float('nan')), dict(started=None),
                                  dict(queue_bound_s='bad')])
def test_queue_sweep_requires_valid_deadline(tmp_path, queued, fields):
    queued(queued_until=9900)
    path, _ = queued('invalid', **fields)
    before = path.read_bytes()
    records = {r['run_id']: r for r in w.read_runs(tmp_path)}
    assert records['run-expired']['reason'] == 'queue_timeout'
    assert records['run-invalid']['status'] == 'queued'
    assert path.read_bytes() == before


@pytest.mark.parametrize('status', ['running', 'cancelled', 'completed'])
def test_queue_sweep_rechecks_under_owner_lock(tmp_path, queued, monkeypatch, status):
    from plangraph.spawner import notify
    path, original = queued(queued_until=9900)
    fresh = dict(original, status=status, job_id='new-job')
    @contextmanager
    def lock(target):
        assert target == path
        path.write_text(json.dumps(fresh))
        yield
    monkeypatch.setattr(notify, 'inbox_lock', lock)
    assert w.read_runs(tmp_path) == [fresh]
    assert json.loads(path.read_text()) == fresh


def test_expired_queue_no_longer_suppresses_completion_or_claim(tmp_path, queued, monkeypatch):
    from plangraph.spawner import runner
    path, _ = queued(queued_until=9900)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: SimpleNamespace(
        retry_completions=lambda: None))
    monkeypatch.setattr(w, 'read_activity', lambda *a: 1)
    monkeypatch.setattr(w, 'read_claims', lambda: [dict(
        key='plangraph:QUEUE', state='claimed', claimed_by='parent')])
    assert w.main(['tick']) == 0
    assert json.loads(path.read_text())['reason'] == 'queue_timeout'
    monkeypatch.setattr(w, 'time', SimpleNamespace(time=lambda: 10401))
    assert w.main(['tick']) == 0
    events = [json.loads(p.read_text()) for p in (tmp_path / 'watchdog/outbox').glob('*.json')]
    assert {e['rule'] for e in events} == {'finished-child', 'idle-claim'}
