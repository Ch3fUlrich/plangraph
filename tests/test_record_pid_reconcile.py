"""Every Python status/list read reconciles its snapshot without disk writes."""
import json
from types import SimpleNamespace

import pytest
from plangraph.spawner.service import SpawnService
from plangraph.spawner import stall_probe


@pytest.mark.parametrize('live,expected', [(False, 'exited'), (True, 'running'), (None, 'running')])
def test_status_and_ps_reconcile_record(tmp_path, monkeypatch, live, expected):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path / 'runs/run-fixture/record.json'
    path.parent.mkdir(parents=True)
    run = dict(run_id='run-fixture', status='running', started=1, pid=321, pid_start=45)
    path.write_text(json.dumps(run))
    before = path.read_bytes()
    calls = []
    monkeypatch.setattr(stall_probe, '_record_pid_live', lambda pid, start:
                        (calls.append((pid, start)), live)[1], raising=False)
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    for row in (service.result('run-fixture'), service.ps()['runs'][0]):
        assert row['status'] == expected
        if not live and live is not None:
            assert row['reason'] == 'pid_gone'
            assert row['ended'] >= row['started']
    assert calls == [(321, 45), (321, 45)]
    assert path.read_bytes() == before


def test_remote_overflow_pid_is_never_queried_on_local_host(tmp_path, monkeypatch):
    path = tmp_path / 'runs/run-remote/record.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(run_id='run-remote', status='running', pid=123,
        overflow_remote=True, host='remote-fixture')))
    monkeypatch.setattr(stall_probe, '_record_pid_live', lambda *a: pytest.fail('remote PID queried locally'))
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    assert service.result('run-remote')['status'] == 'running'


def test_log_age_does_not_claim_a_live_pid_has_exited(tmp_path, monkeypatch):
    import os
    log = tmp_path / 'live.log'
    log.write_text('last output')
    os.utime(log, (10, 10))
    monkeypatch.setattr(stall_probe, '_record_pid_live', lambda *a: True)
    record = stall_probe.reconcile_record({'status': 'running', 'pid': 123}, tmp_path, now=100)
    assert record['live_log_age_s'] == 90
    assert record['status'] == 'running'


def test_wait_allows_supervisor_to_finish_after_pid_disappears(tmp_path, monkeypatch):
    from plangraph.spawner.runner import spawn_service
    service = spawn_service(runner=SimpleNamespace(), state=tmp_path)
    records = iter([
        dict(status='exited', reason='pid_gone', ended=100),
        dict(status='completed', exit_code=0, ended=101),
    ])
    monkeypatch.setattr(service, 'result', lambda _: next(records))
    import asyncio
    final = asyncio.run(service.wait('run-fixture', timeout=1))
    assert final['status'] == 'completed'
    assert final['exit_code'] == 0


@pytest.mark.parametrize('timeout', [None, 0.1])
def test_wait_pid_gone_grace_is_bounded(tmp_path, monkeypatch, timeout):
    import asyncio
    import itertools
    from plangraph.spawner import runner
    service = runner.spawn_service(runner=SimpleNamespace(), state=tmp_path)
    record = dict(status='exited', reason='pid_gone', ended=100)
    monkeypatch.setattr(service, 'result', lambda _: record)
    ticks = itertools.count(1, .1)
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: 100))
    if timeout is None:
        assert asyncio.run(service.wait('run-fixture')) == record
    else:
        with pytest.raises(TimeoutError):
            asyncio.run(service.wait('run-fixture', timeout=timeout))
