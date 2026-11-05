import json
from pathlib import Path

import pytest

from plangraph.spawner import stall_watchdog as w


def test_unknown_reaches_outbox(tmp_path):
    findings = w.tick([dict(run_id='run-fixture')], [], lambda s: None,
                      state=tmp_path, deliver=lambda e: w.deliver_event(tmp_path, e))
    assert findings[0]['rule'] == 'unknown'
    event = json.loads(next((tmp_path / 'watchdog/outbox').glob('*.json')).read_text())
    assert event['rule'] == 'unknown' and 'cancelled' not in event


@pytest.mark.parametrize('age', [301, 5400])
def test_finished_child_redelivered_with_routing_alert(age):
    run = dict(run_id='run-abc', status='completed', ended=10000-age,
               started=1, exit_code=0, parent_session='parent')
    sent = []
    findings = w.tick([run], [], lambda session: 1, now=10000,
                      deliver=sent.append)
    assert [e['kind'] for e in sent] == ['completion', 'alert']
    assert sent[0]['recipient'] == 'parent'
    assert sent[0]['run_id'] == 'run-abc'
    assert sent[1]['recipient'] == 'routing'
    assert w.status(findings).startswith('! stall ')


def test_idle_claim_alerts_owner_and_routing():
    claim = dict(key='plangraph:STALL', state='claimed', claimed_by='owner',
                 session_id='parent', updated_at=1)
    sent = []
    assert w.tick([], [claim], lambda session: 1, now=2000, deliver=sent.append)
    assert [e['recipient'] for e in sent] == ['owner', 'routing']
    assert all(e['kind'] == 'alert' for e in sent)
    assert not w.tick([dict(status='running', item_key=claim['key'])],
                      [claim], lambda session: 1, now=2000)


@pytest.mark.parametrize('activity,ended', [(9900, 9400), (1, 9800), (None, 9400)])
def test_no_false_completion_alert(activity, ended):
    assert not w.tick([dict(run_id='run-a', status='completed', ended=ended,
                           parent_session='p')], [], lambda s: activity, now=10000)


def test_real_readers_and_durable_delivery(tmp_path):
    runs = tmp_path / 'runs/run-a'
    runs.mkdir(parents=True)
    (runs / 'record.json').write_text(json.dumps(dict(run_id='run-a')))
    assert w.read_runs(tmp_path) == [dict(run_id='run-a')]
    activity = tmp_path / 'session-activity/p'
    activity.parent.mkdir()
    activity.touch()
    assert w.read_activity(tmp_path, 'p') == activity.stat().st_mtime
    with pytest.raises(ValueError):
        w.read_activity(tmp_path, '../escape')
    w.deliver_event(tmp_path, dict(kind='completion', recipient='p', run_id='run-a'))
    assert json.loads(next((tmp_path / 'watchdog/outbox').glob('*.json')).read_text())['recipient'] == 'p'
    class Keeper:
        def call(self, method, endpoint):
            assert (method, endpoint) == ('GET', 'snapshot')
            return {'items': [dict(key='plangraph:A', state='claimed')]}
    assert w.read_claims(Keeper())[0]['key'] == 'plangraph:A'


def test_delivery_bridge_acknowledges_only_success(tmp_path):
    w.deliver_event(tmp_path, dict(kind='alert', recipient='routing'))
    def fail(*args, **kwargs):
        raise RuntimeError('offline')
    with pytest.raises(RuntimeError):
        w.flush_outbox(tmp_path, ['bridge'], run=fail)
    assert list((tmp_path / 'watchdog/outbox').glob('*.json'))
    sent = []
    w.flush_outbox(tmp_path, ['bridge'], run=lambda args, **kw: sent.append(json.loads(kw['input'])))
    assert sent == [dict(kind='alert', recipient='routing')]
    assert not list((tmp_path / 'watchdog/outbox').glob('*.json'))


def test_tick_cli_persists_status_and_delivers_w12(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_WATCHDOG_DELIVERY', '["bridge"]')
    monkeypatch.setattr(w.time, 'time', lambda: 10000)
    monkeypatch.setattr(w, 'read_runs', lambda state: [dict(
        run_id='run-w12', status='completed', ended=4600, started=1,
        parent_session='parent', exit_code=0)])
    monkeypatch.setattr(w, 'read_activity', lambda state, session: 1)
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    sent = []
    original = w.flush_outbox
    monkeypatch.setattr(w, 'flush_outbox', lambda state, command: original(
        state, command, run=lambda args, **kw: sent.append(json.loads(kw['input']))))
    assert w.main(['tick']) == 0
    assert sorted(e['kind'] for e in sent) == ['alert', 'completion', 'completion']
    assert '! stall ' in (tmp_path / 'watchdog/status.txt').read_text()
    capsys.readouterr()
    w.main(['status'])
    assert capsys.readouterr().out.startswith('! stall ')


@pytest.mark.parametrize('host', ['linux', 'windows'])
def test_scheduler_installation(tmp_path, host):
    calls = []
    w.install(host, tmp_path, python='/usr/bin/python3', root=Path.cwd(),
              run=lambda args, **kw: calls.append(args))
    content = '\n'.join(p.read_text(encoding='utf-16' if host == 'windows' else 'utf-8') for p in tmp_path.iterdir())
    assert 'plangraph.spawner.stall_watchdog' in content
    if host == 'linux':
        assert 'OnUnitActiveSec=120' in content
        assert ['systemctl', '--user', 'enable', '--now', 'plangraph-stall-watchdog.timer'] in calls
    else:
        assert '<Interval>PT2M</Interval>' in content
        assert calls[0][:2] == ['schtasks', '/Create']
        import re
        workdir = re.search(r'<WorkingDirectory>(.*)</WorkingDirectory>', content).group(1)
        assert (Path(workdir.replace('\\', '/')) / 'plangraph/spawner/stall_watchdog.py').is_file()
    bootstrap = Path('deploy/bootstrap.' + ('sh' if host == 'linux' else 'ps1')).read_text()
    assert 'plangraph.spawner.stall_watchdog' in bootstrap
    assert 'install' in bootstrap


def test_tick_keeper_unconfigured_skips_with_one_warning(tmp_path, monkeypatch):
    from plangraph.keeper.cli_verbs import MissingKeeperURL
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    monkeypatch.setattr(w, "read_runs", lambda state: [])
    monkeypatch.setattr(w, "read_claims", lambda: (_ for _ in ()).throw(MissingKeeperURL()))
    assert w.main(["tick"]) == 0
    assert (tmp_path / "watchdog/status.txt").read_text().count("keeper skipped MissingKeeperURL") == 1


def test_real_spawn_record_parent_and_item_come_from_completion(tmp_path):
    record = Path(__file__).parent / "fixtures/stall_real_spawn_record.json"
    (tmp_path / "runs/run-real").mkdir(parents=True)
    (tmp_path / "runs/run-real/record.json").write_text(record.read_text())
    [run] = w.read_runs(tmp_path)
    assert (run["parent_session"], run["item_key"]) == ("build-L1", "plangraph:STALL-WATCHDOG")
    assert w.tick([run], [], lambda s: 1, now=run["ended"] + 400)[0]["rule"] == "finished-child"
def test_host_run_probes(tmp_path, monkeypatch):
    import os
    from plangraph.spawner.stall_watchdog import tick
    from plangraph.spawner import runner
    directory = tmp_path / 'runs/run-fixture'
    directory.mkdir(parents=True)
    log = directory / 'live.log'
    log.write_text('work\n')
    record = dict(run_id='run-fixture', status='running', pid=os.getpid(),
                  started=0, role='implement', leg='fixture', card={'stall_min': 1})
    cancelled = []
    class Service:
        async def cancel(self, run_id):
            cancelled.append(run_id)
            return {'status': 'cancelled'}
    monkeypatch.setattr(runner, 'spawn_service', lambda **kw: Service())
    def check(now):
        return tick([record], [], lambda _: None, now=now, state=tmp_path)
    os.utime(log, (100, 100))
    assert check(159) == []
    assert check(160)[0]['rule'] == 'run-silent'
    assert not cancelled
    assert check(220)[0]['cancelled']
    assert cancelled == ['run-fixture']
    os.utime(log, (220, 220))
    assert check(221) == []
    log.unlink()
    assert check(500)[0]['rule'] == 'unknown'
    assert len(cancelled) == 1
    log.write_text('the retry is running in the background\n')
    record.update(status='completed', ended=230)
    assert check(500)[0]['rule'] == 'run-ended-no-commit'
    record.update(reason='writer_no_changes')
    log.write_text('done\n')
    assert check(500)[0]['rule'] == 'run-ended-no-commit'
    record.update(status='running')
    def gone(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'kill', gone)
    assert check(500)[0]['rule'] == 'run-orphan'
    import json
    assert json.loads((directory / 'record.json').read_text())['reason'] == 'orphaned'
