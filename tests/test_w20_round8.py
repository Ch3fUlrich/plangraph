import asyncio
import json

import pytest

from plangraph.planner.routing import SpawnRefused, validate_model
from plangraph.spawner import resolver, stall_watchdog
from plangraph.spawner.runner import spawn_service


@pytest.mark.parametrize('fields', [{}, {'available': 1}, {'active': True},
    {'available': 0}, {'available': 1, 'active': False},
    {'available': 1, 'active': True}])
def test_catalog_admission_exact(monkeypatch, fields):
    row = dict(id='test/admission', aa_intelligence=50, **fields)
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [row])
    admitted = fields.get('available') == 1 and fields.get('active') is True
    if admitted:
        assert validate_model(row['id']) == row['id']
    else:
        with pytest.raises(SpawnRefused):
            validate_model(row['id'])
    assert bool(resolver.rank_candidates([row])) is admitted


def test_marker_failure_sink_exactly_once(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    service = spawn_service(state=tmp_path)
    write = private_io.atomic_write_private
    def fail_marker(path, text):
        if str(path).endswith('.delivered'):
            raise OSError('marker unavailable')
        return write(path, text)
    monkeypatch.setattr(private_io, 'atomic_write_private', fail_marker)
    service._save(dict(run_id='run-abc', started=1, ended=2, status='completed', exit_code=0))
    service.retry_completions()
    events = [json.loads(s) for s in (tmp_path / 'inbox/parent.jsonl').read_text().splitlines()]
    assert len(events) == 1
    assert events[0]['idempotency_key'] == 'run-abc:completed'


def test_corrupt_runs_skipped_and_recorded(tmp_path):
    path = tmp_path / 'runs/run-bad/record.json'
    path.parent.mkdir(parents=True)
    path.write_text('{')
    assert spawn_service(state=tmp_path).ps()['runs'] == []
    assert stall_watchdog.read_runs(tmp_path) == []
    assert 'run-bad' in (tmp_path / 'watchdog/bad-records.json').read_text()


def test_finished_child_not_reenqueued_after_ack(tmp_path):
    event = dict(rule='finished-child', run_id='run-abc', kind='completion', recipient='parent')
    stall_watchdog.deliver_event(tmp_path, event, now=1)
    next((tmp_path / 'watchdog/outbox').glob('*.json')).unlink()
    stall_watchdog.deliver_event(tmp_path, event, now=4000)
    assert not list((tmp_path / 'watchdog/outbox').glob('*.json'))


def test_historical_delivered_completion_not_requeued(tmp_path):
    service = spawn_service(state=tmp_path)
    service._save(dict(run_id='run-old', started=1, ended=2, status='completed', exit_code=0,
                       parent_session='parent'))
    runs = stall_watchdog.read_runs(tmp_path)
    events = []
    stall_watchdog.tick(runs, [], lambda s: 0, now=4000, deliver=events.append)
    assert events == []


def test_route_refusal_persisted(tmp_path):
    service = spawn_service(state=tmp_path)
    with pytest.raises(SpawnRefused):
        service.route(None)
    assert service.ps()['runs'][0]['exit_code'] == 2


@pytest.mark.parametrize('exc,code', [(ValueError('bad'), 1), (SpawnRefused('bad'), 2)])
def test_refusal_record_code(tmp_path, exc, code):
    assert spawn_service(state=tmp_path).save_refusal(exc)['exit_code'] == code


def test_override_writer_family_excluded(tmp_path):
    from plangraph.planner.legs import DEFAULT_LEGS
    leg = DEFAULT_LEGS[0]
    service = spawn_service(state=tmp_path)
    with pytest.raises(SpawnRefused, match='excluded writer family'):
        asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x', role='review',
            model=leg.model, writer_families=[leg.family], override_reason='operator selection', dry_run=True))


def test_supervisor_refusal_reason_and_code(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    payload = dict(state=str(tmp_path), card={'estimate_tokens': 100}, prompt='x',
                   leg='missing', override_reason='operator selection')
    child = subprocess.run([sys.executable, '-m', 'plangraph.spawner.cli', '--supervise'],
                           input=json.dumps(payload) + '\n', text=True, capture_output=True,
                           env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src')))
    assert child.returncode == 2
    record = json.loads(child.stdout)
    assert record['exit_code'] == 2
    assert record['error'] == 'unknown leg'


@pytest.mark.parametrize('exc,code', [(ValueError('bad'), 1), (SpawnRefused('bad'), 2)])
def test_cli_mcp_record_exit_parity(tmp_path, monkeypatch, exc, code):
    from plangraph.spawner import cli
    from plangraph.spawner.mcp import MCPSpawnerServer
    service = spawn_service(state=tmp_path)
    def fail(*args, **kwargs):
        service.save_refusal(exc)
        raise exc
    monkeypatch.setattr(service, 'route', fail)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    assert cli.main(['route', '--card', '{"estimate_tokens":100}']) == code
    server = MCPSpawnerServer(state=tmp_path)
    monkeypatch.setattr(server.service, 'route', fail)
    result = asyncio.run(server.handle_rpc_request(dict(jsonrpc='2.0', id=1,
        method='tools/call', params=dict(name='plangraph_agent_route',
                                       arguments={'card': {'estimate_tokens': 100}}))))
    assert result['error']['data']['exit_code'] == code
    assert all(r['exit_code'] == code for r in service.ps()['runs'])
