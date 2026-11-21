"""A run waiting for room is owned and re-checked; it starts, is refused, or is reported stale (LE-SPAWNER-QUEUE-STUCK-AFTER-FLOOR-CLEARS)."""
import asyncio
import json
import subprocess
import sys
import threading
import time

import pytest

from registry_route_util import use_projected_overflow_config

from plangraph.spawner import overflow, seat_spawn
from plangraph.spawner.service import SpawnService
from plangraph.spawner.stall_watchdog import sweep_runs


@pytest.fixture
def room(tmp_path, monkeypatch):
    """A service on a private state dir whose local memory the test scripts."""
    use_projected_overflow_config(monkeypatch, tmp_path / 'absent.json')
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'local_limits', lambda registry, role: (2048, 3000))
    service = SpawnService(state=tmp_path / 'state')
    service.runner.default_cwd = str(tmp_path)
    readings = []
    monkeypatch.setattr(seat_spawn, 'resources', lambda svc: (0, readings.pop(0) if len(readings) > 1 else readings[0]))
    return service, readings


def queued_records(service):
    return [r for r in service.ps()['runs'] if r.get('status') == 'queued']


def test_run_queued_under_the_floor_starts_within_60s_after_it_clears(room, monkeypatch):
    service, readings = room
    readings[:] = [4000, 4000, 9000]   # below floor+role (5048), then room
    monkeypatch.setenv(seat_spawn.ADMISSION_WAIT_ENV, '600')
    slept = []
    def sleep(seconds):
        slept.append(seconds)
        assert queued_records(service), 'the waiting run is visible as queued with its owner'
        assert queued_records(service)[0]['owner_pid']
    monkeypatch.setattr(seat_spawn, '_sleep', sleep)
    assert seat_spawn.admit(service, {'role': 'review'}) is None   # admitted: the caller goes on to start the run
    assert sum(slept) <= 60 and len(slept) == 2 and max(slept) <= 60
    assert service.ps()['runs'] == []   # the waiting record is gone, not left queued


def test_run_without_room_is_refused_at_the_end_of_its_window_never_left_queued(room, monkeypatch):
    service, readings = room
    readings[:] = [4000]
    monkeypatch.setenv(seat_spawn.ADMISSION_WAIT_ENV, '0.2')
    monkeypatch.setenv(seat_spawn.ADMISSION_POLL_ENV, '0.01')
    record = seat_spawn.admit(service, {'role': 'review'})
    assert record['status'] == 'refused' and record['exit_code'] == 2
    assert 'memory: free 4000 MB' in record['reason'] and 'admission window' in record['error']
    assert 'owner_pid' not in record
    assert service.result(record['run_id'])['status'] == 'refused'
    assert queued_records(service) == []


def test_cancel_of_a_queued_run_sets_cancelled_and_ends_the_wait(room, monkeypatch):
    service, readings = room
    readings[:] = [4000]
    monkeypatch.setenv(seat_spawn.ADMISSION_WAIT_ENV, '30')
    monkeypatch.setenv(seat_spawn.ADMISSION_POLL_ENV, '0.01')
    outcome = []
    waiter = threading.Thread(target=lambda: outcome.append(seat_spawn.admit(service, {'role': 'review'})))
    waiter.start()
    deadline = time.time() + 10
    while not queued_records(service) and time.time() < deadline:
        time.sleep(0.01)
    run_id = queued_records(service)[0]['run_id']
    assert asyncio.run(service.cancel(run_id))['status'] == 'cancelled'
    waiter.join(timeout=10)
    assert not waiter.is_alive()
    assert outcome[0]['status'] == 'cancelled' and outcome[0]['run_id'] == run_id
    assert service.result(run_id)['status'] == 'cancelled'


def dead_pid():
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    return child.pid


def write_record(service, **fields):
    record = dict(run_id='run-' + 'a' * 32, status='queued', started=time.time(), role='review',
                  reason='memory: free 4091 MB, floor 4096 MB', exit_code=5, **fields)
    path = service._path(record['run_id'])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    return record['run_id']


@pytest.mark.parametrize('fields', [{}, {'owner_pid': 'dead'}])
def test_queued_record_without_a_live_owner_is_stale_in_ps_and_status(tmp_path, fields):
    service = SpawnService(state=tmp_path / 'state')
    if fields:
        fields = {'owner_pid': dead_pid()}
    run_id = write_record(service, **fields)
    [row] = service.ps()['runs']
    assert row['stale'] is True and row['stale_reason']
    shown = asyncio.run(service.refresh_result(run_id))
    assert shown['stale'] is True and shown['status'] == 'queued'
    assert 'stale' not in service.result(run_id)   # shown, not persisted


def test_a_live_owner_is_not_stale(tmp_path):
    import os
    service = SpawnService(state=tmp_path / 'state')
    write_record(service, owner_pid=os.getpid())
    [row] = service.ps()['runs']
    assert 'stale' not in row


def test_sweep_refuses_a_queued_record_whose_owner_died(tmp_path):
    service = SpawnService(state=tmp_path / 'state')
    run_id = write_record(service, owner_pid=dead_pid())
    [row] = sweep_runs(service.state)
    assert row['status'] == 'refused' and row['reason'] == 'queue_owner_gone' and row['exit_code'] == 2
    assert service.result(run_id)['status'] == 'refused'


def test_cancel_of_an_ownerless_queued_record_sets_cancelled(tmp_path):
    service = SpawnService(state=tmp_path / 'state')
    run_id = write_record(service)
    assert asyncio.run(service.cancel(run_id))['status'] == 'cancelled'
    assert service.result(run_id)['status'] == 'cancelled'


@pytest.mark.parametrize('room_opens', [True, False])
def test_cancel_during_the_poll_sleep_is_never_overwritten_or_started(room, monkeypatch, room_opens):
    service, readings = room
    readings[:] = [4000, 9000] if room_opens else [4000]
    monkeypatch.setenv(seat_spawn.ADMISSION_WAIT_ENV, '600' if room_opens else '0.3')
    monkeypatch.setenv(seat_spawn.ADMISSION_POLL_ENV, '0.01')
    def cancel_in_sleep(seconds):
        [row] = queued_records(service)
        asyncio.run(service.cancel(row['run_id']))
        time.sleep(0.4 if not room_opens else 0)
    monkeypatch.setattr(seat_spawn, '_sleep', cancel_in_sleep)
    record = seat_spawn.admit(service, {'role': 'review'})
    assert record is not None and record['status'] == 'cancelled'
    assert service.result(record['run_id'])['status'] == 'cancelled'
