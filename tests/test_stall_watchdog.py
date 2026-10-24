import json
from pathlib import Path

import pytest

from plangraph.spawner import stall_watchdog as w


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
    assert sorted(e['kind'] for e in sent) == ['alert', 'completion']
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
    bootstrap = Path('deploy/bootstrap.' + ('sh' if host == 'linux' else 'ps1')).read_text()
    assert 'plangraph.spawner.stall_watchdog' in bootstrap
    assert 'install' in bootstrap
