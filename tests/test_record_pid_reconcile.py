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
