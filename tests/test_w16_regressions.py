import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from plangraph.planner.routing import SpawnRefused
try:
    from plangraph.spawner import stall_watchdog as w
except ImportError:
    from plangraph import stall_watchdog as w
from plangraph.spawner.runner import spawn_service


def validate_model(model):
    from plangraph.planner.routing import validate_model as validate
    return validate(model)


@pytest.mark.parametrize('host', ['linux', 'windows'])
def test_bootstrap_watchdog_default_root(tmp_path, host):
    w.install(host, tmp_path, run=lambda *a, **k: None)
    root = Path(__file__).resolve().parents[1]
    text = ''.join(p.read_text() for p in tmp_path.iterdir())
    assert str(root) in text
    assert str(root / 'src/src') not in text
    bootstrap = (root / 'deploy' / ('bootstrap.sh' if host == 'linux' else 'bootstrap.ps1')).read_text()
    assert 'plangraph.spawner.stall_watchdog' in bootstrap


def test_watchdog_ack_does_not_reset_realert_interval(tmp_path):
    event = dict(kind='alert', recipient='routing')
    w.deliver_event(tmp_path, event, now=100, interval=1000)
    next((tmp_path / 'watchdog/outbox').glob('*.json')).unlink()
    w.deliver_event(tmp_path, event, now=101, interval=1000)
    assert not list((tmp_path / 'watchdog/outbox').glob('*.json'))
    w.deliver_event(tmp_path, event, now=1100, interval=1000)
    assert len(list((tmp_path / 'watchdog/outbox').glob('*.json'))) == 1


def test_watchdog_malformed_parent_records_and_continues(tmp_path):
    runs = [dict(run_id='run-bad', parent_session='../bad', ended=1, status='completed'),
            dict(run_id='run-ok', parent_session='ok', ended=1, status='completed')]
    def activity(session):
        w.read_activity(tmp_path, session)
        return 1
    sent = []
    findings = w.tick(runs, [], activity, now=1000, deliver=sent.append)
    assert findings[0]['rule'] == 'invalid-parent-session'
    assert len(sent) == 2


def test_exact_catalog_allowlist():
    from plangraph.spawner.resolver import get_catalog_models
    from plangraph.planner.legs import DEFAULT_LEGS
    ids = {r['id'] for r in get_catalog_models()} | {l.model for l in DEFAULT_LEGS}
    for model in ids:
        assert validate_model(model) == model


def malformed_ids():
    word = 'mi' + 'mo'
    suffix = '-v2.6-flash'
    return [prefix + word + suffix for prefix in ('cx_', '2', 'x', '@', '+', ' ', 'omniroute/') ] + [
        word + tail for tail in ('flash', 'pro', '.v2', '_v2', ',v2.6-flash', '|v2.6-flash',
                                 '(v2.6-flash)', ' v2.6-flash', '+v2.6-flash', '@v2.6-flash', suffix)] + [
        'm\u0456' + 'mo' + suffix, 'mi' + 'm\u043e' + suffix,
        'mi\u200b' + 'mo' + suffix, word.upper() + suffix,
        'omniroute/' + word + 'v2.6-flash']


@pytest.mark.parametrize('model', malformed_ids())
def test_unknown_variants_refused_by_route_spawn_override(tmp_path, model):
    from dataclasses import replace
    from plangraph.planner.routing import plan_spawn, hand_picked
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner.runner import SpawnerRunner
    with pytest.raises(SpawnRefused):
        validate_model(model)
    with pytest.raises(SpawnRefused):
        plan_spawn({'estimate_tokens': 100}, role='implement', legs=(replace(DEFAULT_LEGS[0], model=model),))
    with pytest.raises(SpawnRefused):
        asyncio.run(SpawnerRunner().spawn('p', model_or_combo=model, dry_run=True))
    with pytest.raises(SpawnRefused):
        hand_picked(model, override_reason='explicit regression override', log_path=tmp_path / 'audit')


def test_refusal_preserved_on_non_oserror(tmp_path):
    service = spawn_service(state=tmp_path)
    service._save = Mock(side_effect=TypeError('serialization'))
    error = SpawnRefused('original')
    assert service.save_refusal(error)['exit_code'] == 2
    assert 'TypeError' in error.__notes__[0]


def test_mcp_missing_prompt_persists_keyerror(tmp_path):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path)
    with pytest.raises(KeyError):
        asyncio.run(server.call_tool('plangraph_agent_spawn', {'card': {'estimate_tokens': 100}}))
    record, = server.service.ps()['runs']
    assert record['refusal'] == 'KeyError'
    assert record['exit_code'] == 2


def test_terminal_saved_before_event_builder(tmp_path, monkeypatch):
    service = spawn_service(state=tmp_path)
    monkeypatch.setattr('plangraph.spawner.notify.completion_event', Mock(side_effect=ValueError('event')))
    record = dict(run_id='run-terminal', status='completed', exit_code=0, started=1, ended=2)
    with pytest.raises(ValueError):
        service._save(record)
    assert service.result(record['run_id'])['ended'] == 2


def test_sink_failure_retries_without_duplicate_event(tmp_path):
    sink = Mock()
    sink.deliver.side_effect = [RuntimeError('offline'), None, None]
    service = spawn_service(state=tmp_path, completion_sink=sink)
    record = dict(run_id='run-retry', status='completed', exit_code=0, started=1, ended=2)
    service._save(record)
    service._save(record)
    assert sink.deliver.call_count == 3
    assert len({c.args[1]['event_id'] for c in sink.deliver.call_args_list}) == 1


def test_completion_uses_record_sandbox(tmp_path, monkeypatch):
    service = spawn_service(state=tmp_path)
    seen = []
    monkeypatch.setattr('plangraph.spawner.notify.subprocess.check_output',
                        lambda *a, **kw: seen.append(kw['cwd']) or 'sandbox-head')
    service._save(dict(run_id='run-cwd', status='completed', exit_code=0, started=1, ended=2, cwd='sandbox'))
    assert seen == ['sandbox']
    assert service.result('run-cwd')['completion']['head_sha'] == 'sandbox-head'


@pytest.mark.parametrize('script,rc', [('raise SystemExit(0)', 0), ('raise SystemExit(7)', 7),
    ('import os,signal; os.kill(os.getpid(), signal.SIGKILL)', -9),
    ('import time; time.sleep(60)', 124)])
def test_automatic_runner_delivery(tmp_path, monkeypatch, script, rc):
    import sys
    from plangraph.spawner.runner import SpawnerRunner
    if rc == 124:
        monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '.03')
    monkeypatch.setattr('plangraph.spawner.runner.build_command', lambda **kw: [sys.executable, '-c', script])
    service = spawn_service(runner=SpawnerRunner(), state=tmp_path)
    async def run():
        record = await service.spawn(card={'estimate_tokens': 100}, prompt='test', wait_seconds=None)
        assert record['exit_code'] == rc
        for recipient in ('parent', 'keeper'):
            events = (tmp_path / 'inbox' / (recipient + '.jsonl')).read_text().splitlines()
            assert len(events) == 1
            assert json.loads(events[0])['rc'] == rc
    asyncio.run(run())


def test_invalid_timeout_kills_child(tmp_path, monkeypatch):
    import sys
    from plangraph.spawner.runner import SpawnerRunner
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', 'invalid')
    monkeypatch.setattr('plangraph.spawner.runner.build_command', lambda **kw: [sys.executable, '-c', 'import time; time.sleep(60)'])
    async def run():
        runner = SpawnerRunner()
        job = await runner.spawn('p', 'mimo/mimo-v2.6-flash')
        await runner._tasks[job.job_id]
        assert job.exit_code == 124
        assert job.events == [{'event': 'run_timeout'}]
    asyncio.run(run())


def test_spawn_executable_entrypoint():
    import subprocess
    result = subprocess.run([str(Path('tools/plangraph-spawn').resolve()), '--help'], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'wait' in result.stdout


def test_acp_transport_does_not_infer_harness(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    monkeypatch.setattr('plangraph.spawner.resolver.get_catalog_models', lambda: [
        dict(id='test/custom', tier='paid', context_tokens=131072, provider_gateway_mode='omniroute_acp')])
    with pytest.raises(SpawnRefused, match='registry data'):
        asyncio.run(SpawnService(state=tmp_path).spawn(card={'estimate_tokens': 100},
            model='test/custom', override_reason='explicit regression override', dry_run=True))


def test_steer_drain_failure_not_delivered():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from plangraph.spawner.runner import SpawnerRunner, JobInfo
    runner = SpawnerRunner()
    job = JobInfo('one', 'running', '', '')
    runner.jobs['one'] = job
    stdin = Mock()
    stdin.is_closing.return_value = False
    stdin.drain = AsyncMock(side_effect=BrokenPipeError)
    runner._processes['one'] = SimpleNamespace(stdin=stdin)
    assert not asyncio.run(runner.steer('one', 'hello'))['steered']
    assert job.steer_messages == ['hello']
    stdin.write.assert_called_once()


def test_nonce_capacity_typed():
    from plangraph.spawner.runner import SpawnerRunner, JobInfo, NonceCapacity
    runner = SpawnerRunner()
    runner.jobs = {str(i): JobInfo(str(i), 'running', '', '') for i in range(4096)}
    runner._nonces = {key: key for key in runner.jobs}
    with pytest.raises(NonceCapacity):
        asyncio.run(runner.spawn('p', 'mimo/mimo-v2.6-flash', dry_run=True))


def test_skill_frontmatter_has_immediate_name():
    assert Path('skills/plangraph-planning/SKILL.md').read_text().splitlines()[:2] == ['---', 'name: plangraph-planning']


def test_watchdog_automatically_redrives_failed_sink(tmp_path, monkeypatch):
    from plangraph.spawner.notify import FileInbox
    service = spawn_service(state=tmp_path)
    original = FileInbox.deliver
    monkeypatch.setattr(FileInbox, 'deliver', Mock(side_effect=RuntimeError('offline')))
    service._save(dict(run_id='run-redrive', job_id='job-one', status='completed', exit_code=0, started=1, ended=2))
    assert not (tmp_path / 'inbox/parent.jsonl').exists()
    monkeypatch.setattr(FileInbox, 'deliver', original)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    assert w.main(['tick']) == 0
    assert len((tmp_path / 'inbox/parent.jsonl').read_text().splitlines()) == 1
    assert w.main(['tick']) == 0
    assert len((tmp_path / 'inbox/parent.jsonl').read_text().splitlines()) == 1


@pytest.mark.parametrize('model', malformed_ids())
def test_unknown_variants_cli_mcp(tmp_path, model):
    from plangraph.spawner.cli import main
    from plangraph.spawner.mcp import MCPSpawnerServer
    assert main(['--state', str(tmp_path), 'spawn', 'p', '--card', '{"estimate_tokens":100}',
                 '--model', model, '--override-reason', 'explicit regression override', '--dry-run']) == 2
    server = MCPSpawnerServer(state=tmp_path)
    response = asyncio.run(server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'plangraph_agent_spawn', 'arguments': {'card': {'estimate_tokens': 100},
        'prompt': 'p', 'model_or_combo': model, 'override_reason': 'explicit regression override', 'dry_run': True}}}))
    assert response['error']['data']['type'] == 'SpawnRefused'


def test_stdio_internal_and_unknown_tool_errors(tmp_path, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path)
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'unknown'}}
    assert asyncio.run(server.handle_rpc_request(request))['error']['code'] == -32602
    async def broken(*args):
        raise RuntimeError('internal')
    monkeypatch.setattr(server, 'call_tool', broken)
    assert asyncio.run(server.handle_rpc_request(request))['error']['code'] == -32603


def test_leg_sqlite_private_before_connect(tmp_path, monkeypatch):
    import sqlite3
    from plangraph.spawner.leg_state import LegStateStore
    path = tmp_path / 'legs.sqlite'
    connect = sqlite3.connect
    def checked(*args, **kwargs):
        assert path.stat().st_mode & 0o777 == 0o600
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', checked)
    with LegStateStore(path)._connect():
        pass


def test_selection_skip_keeps_window_evidence():
    from dataclasses import replace
    from plangraph.planner.routing import plan_spawn
    from plangraph.planner.legs import DEFAULT_LEGS
    leg = replace(next(l for l in DEFAULT_LEGS if 'implement' in l.roles), max_request_tokens=1)
    plan = plan_spawn({'estimate_tokens': 100}, role='implement', legs=(leg,))
    assert plan.skipped[0]['evidence']['error']


def test_plan_error_is_not_provider_exhaustion(tmp_path, monkeypatch):
    from plangraph.planner.routing import SpawnPlan
    service = spawn_service(state=tmp_path)
    monkeypatch.setattr(service, 'route', lambda *a, **k: SpawnPlan((), (), 'operator-order', (), (), error='invalid order'))
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}))
    assert record['exit_code'] == 2
    assert record['status'] == 'refused'
    assert record['error'] == 'invalid order'


def test_ast_valueerror_is_unknown(tmp_path, monkeypatch):
    from plangraph.spawner.measure import ast_caller_fanout
    (tmp_path / 'changed.py').write_text('')
    (tmp_path / 'caller.py').write_text('import changed')
    monkeypatch.setattr('plangraph.spawner.measure.ast.parse', Mock(side_effect=ValueError('null')))
    assert ast_caller_fanout(tmp_path, ['changed.py']) is None
