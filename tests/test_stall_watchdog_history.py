import json
import os

import pytest

from plangraph.spawner import stall_watchdog as w


def records(state, count=1, **fields):
    for i in range(count):
        path = state / f'runs/run-{i}/record.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(run_id=f'run-{i}', **fields)))
    return w.read_runs(state, dry_run=True)


def test_old_history(tmp_path):
    runs = records(tmp_path, 1000, status='failed', started=0, ended=1, pid=None)
    assert w.tick(runs, [], lambda _: None, now=600000, state=tmp_path) == []


@pytest.mark.parametrize('ended,expected', [(96400, 1), (10000, 0), (None, 0),
                                          (100001, 0), ('bad', 0), (float('nan'), 0)])
def test_finished_horizon(tmp_path, ended, expected):
    runs = records(tmp_path, status='failed', role='implement',
                   reason='writer_no_changes', ended=ended)
    findings = w.tick(runs, [], lambda _: None, now=100000, state=tmp_path)
    assert [f['rule'] for f in findings] == ['run-ended-no-commit'] * expected


def test_live_evidence(tmp_path, monkeypatch):
    runs = records(tmp_path, status='running', started=0, pid=123)
    monkeypatch.setattr(os, 'kill', lambda *args: None)
    assert w.probe_run(tmp_path, runs[0], 600000)['rule'] == 'unknown'
    def gone(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'kill', gone)
    assert w.probe_run(tmp_path, runs[0], 600000)['rule'] == 'run-orphan'


def test_dry_run_and_cap(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import runner
    records(tmp_path, 30, status='running', started=0, pid=123)
    monkeypatch.setattr(os, 'kill', lambda *args: None)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_WATCHDOG_DELIVERY', raising=False)
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: pytest.fail('service called'))
    (tmp_path / 'runs/run-bad').mkdir()
    (tmp_path / 'runs/run-bad/record.json').write_text('{')
    log = tmp_path / 'runs/run-0/live.log'
    log.write_text('quiet tool')
    os.utime(log, (1, 1))
    def snapshot():
        return {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    before = snapshot()
    assert w.main(['tick', '--dry-run']) == 0
    assert snapshot() == before
    output = capsys.readouterr().out
    assert 'unknown: 30' in output and 'run-silent: 1' in output
    (tmp_path / 'runs/run-bad/record.json').unlink()
    log.unlink()
    class Service:
        def retry_completions(self):
            pass
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: Service())
    for expected in (26, 31):
        assert w.main(['tick']) == 0
        events = [json.loads(p.read_text()) for p in (tmp_path / 'watchdog/outbox').glob('*')]
        assert len(events) == expected
        assert sum(e['rule'] == 'findings-capped' for e in events) == 1
