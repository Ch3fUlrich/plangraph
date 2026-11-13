"""SPAWNER-SEAT-CWD: local Git checkouts and fake transport, never live seats."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner import cli, mcp, notify, resolver, seat_spawn
from plangraph.spawner.runner import SpawnerRunner, spawn_service


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    for name in ('GIT_CONFIG_PARAMETERS', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        monkeypatch.delenv(name, raising=False)
    for kind in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv(f'GIT_{kind}_NAME', 'Test')
        monkeypatch.setenv(f'GIT_{kind}_EMAIL', 'test@example.invalid')
    repos = [tmp_path / name for name in ('caller', 'reviewed')]
    for repo in repos:
        repo.mkdir()
        git(repo, 'init', '-q', '--template=')
        git(repo, 'commit', '--no-gpg-sign', '--allow-empty', '-qm', repo.name)
        (repo / 'change.txt').write_text('reviewed change\n')
        git(repo, 'add', 'change.txt')
        git(repo, 'commit', '--no-gpg-sign', '-qm', 'change')
    assert git(repos[0], 'rev-parse', 'HEAD') != git(repos[1], 'rev-parse', 'HEAD')
    monkeypatch.chdir(repos[0])
    return repos


@pytest.fixture
def service(tmp_path, monkeypatch, checkouts):
    calls = []
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'mimo-v2.6-flash')
    plan = SpawnPlan((chosen,), (), 'test', (chosen,), ())
    text = 'Review complete\nTEST_GAMING: no\nVERDICT: ACCEPT'

    class Process:
        pid = None
        returncode = 0

        async def communicate(self, input=None):
            if getattr(service, 'advance_head', False):
                git(checkouts[1], 'commit', '--no-gpg-sign', '--allow-empty', '-qm', 'completion head')
            job_id = next(reversed(runner.jobs))
            runner.served_witness(job_id, chosen.model,
                                  adapter_token=runner._adapter_tokens[job_id])
            return ('\n'.join(json.dumps(event) for event in [
                {'type': 'text', 'part': {'text': text}},
                {'type': 'step_finish', 'part': {'reason': 'stop'}}]).encode(), b'')

    async def factory(*args, **kwargs):
        calls.append(kwargs['cwd'])
        return Process()

    async def available(*args, **kwargs):
        return None

    async def launch(plan, **kwargs):
        return plan

    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'test-credential')
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    monkeypatch.setattr(resolver, 'host_skip_reason_async', available)
    monkeypatch.setattr(resolver, 'plan_for_launch_async', launch)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', available)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.witness', lambda *a: None)
    monkeypatch.setattr(seat_spawn, 'admit', lambda *a, **kw: None)
    monkeypatch.setattr(notify, 'deliver_verdict', lambda *a: None)
    runner = SpawnerRunner(process_factory=factory)
    service = spawn_service(runner, state=tmp_path / 'state',
                            keeper_sink=notify.FileInbox(tmp_path / 'state'))
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)

    class Input(io.StringIO):
        def close(self):
            pass

    class Supervisor:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stdout = Input(), self

        def readline(self):
            payload = json.loads(self.stdin.getvalue())
            payload.pop('state')
            return json.dumps(asyncio.run(service.spawn(**payload, wait_seconds=None)))

        def close(self):
            pass

    monkeypatch.setattr(cli, '_start_supervisor', Supervisor)
    service.launch_cwds = calls
    return service


@pytest.mark.parametrize('source', ['flag', 'card', 'both', 'default', 'mcp'])
@pytest.mark.parametrize('dispatch', ['routed', 'explicit', 'seats'])
def test_spawn_completion_uses_selected_checkout(service, checkouts, capsys, tools_importable, source, dispatch):
    caller, reviewed = checkouts
    card = {'estimate_tokens': 1000, 'item_key': 'demo:CWD', 'diff_ref': {
        'repo': str(reviewed), 'base': git(reviewed, 'rev-parse', 'HEAD~1'),
        'head': git(reviewed, 'rev-parse', 'HEAD')}}
    if source in ('card', 'both', 'mcp'):
        card['cwd'] = str(reviewed)
    service.advance_head = source == 'both'
    chosen = 'mimo-v2.6-flash'
    if source == 'mcp':
        server = mcp.MCPSpawnerServer(service.runner, state=service.state)
        server.service = service
        result = asyncio.run(server.call_tool('plangraph_agent_spawn', dict(
            card=card, prompt='Review the checkout', role='review', writer_families=['gpt'],
            wait_seconds=None, seats=dispatch == 'seats',
            **({'model_or_combo': 'mimo/mimo-v2.6-flash', 'override_reason': 'hermetic checkout test'}
               if dispatch == 'explicit' else {}))))
    else:
        args = ['spawn', 'Review the checkout', '--role', 'review', '--writer-family', 'gpt',
                '--card', json.dumps(card)]
        if source in ('flag', 'both'):
            args += ['--cwd', str(reviewed)]
        if dispatch == 'explicit':
            args += ['--leg', chosen, '--override-reason', 'hermetic checkout test']
        if dispatch == 'seats':
            args += ['--seats']
        assert cli.main(args) == 0
        result = json.loads(capsys.readouterr().out)
    record = result['seats'][0] if dispatch == 'seats' else result
    expected = str(caller if source == 'default' else reviewed)
    assert record['status'] == 'completed', record.get('error')
    assert service.launch_cwds == [expected]
    assert record['cwd'] == service.runner.jobs[record['job_id']].cwd == expected
    assert record['served_model'] != 'unwitnessed'
    assert record['completion']['head_sha'] == git(expected, 'rev-parse', 'HEAD')
    assert service.result(record['run_id']) == record
    assert service.runner.default_cwd == str(caller) and Path.cwd() == caller
    if source == 'both':
        assert record['completion']['head_sha'] != card['diff_ref']['head']
    if dispatch != 'explicit':  # Pinned seats remain ineligible for posting.
        import seat_verdict
        argv = ['--key', 'demo:CWD', '--seat-dir', str(service._path(record['run_id']).parent),
                '--writer-family', 'openai', '--token-dir', str(service.state), '--dry-run']
        assert seat_verdict.main(argv + ['--head', record['completion']['head_sha']]) == 0
        capsys.readouterr()
        if source != 'default':
            assert seat_verdict.main(argv + ['--head', git(caller, 'rev-parse', 'HEAD')]) == 2
            assert 'does not match --head' in capsys.readouterr().err


@pytest.mark.parametrize('source', ['flag', 'card', 'mcp'])
@pytest.mark.parametrize('bad', ['relative', 'missing', 'non-git', 'file', 'bare'])
def test_invalid_cwd_refused(service, tmp_path, capsys, source, bad):
    path = tmp_path / bad
    if bad == 'non-git':
        path.mkdir()
    elif bad == 'file':
        path.write_text('not a directory')
    elif bad == 'bare':
        git(tmp_path, 'init', '--bare', '-q', str(path))
    value = 'relative' if bad == 'relative' else str(path)
    card = {'estimate_tokens': 1000, **({'cwd': value} if source != 'flag' else {})}
    if source == 'mcp':
        server = mcp.MCPSpawnerServer(service.runner, state=service.state)
        server.service = service
        response = asyncio.run(server.handle_rpc_request(dict(jsonrpc='2.0', id=1,
            method='tools/call', params=dict(name='plangraph_agent_spawn', arguments=dict(
                card=card, prompt='Review', role='review', writer_families=['gpt'], dry_run=True)))))
        assert response['error']['code'] == -32602
        error = response['error']['message']
    else:
        args = ['spawn', 'Review', '--role', 'review', '--writer-family', 'gpt',
                '--card', json.dumps(card), '--dry-run']
        assert cli.main(args + (['--cwd', value] if source == 'flag' else [])) == 2
        error = capsys.readouterr().err
    assert 'cwd' in error and ('absolute' if bad == 'relative' else
                               'existing' if bad in ('missing', 'file') else 'git checkout') in error
    assert not service.runner.jobs and not service.launch_cwds


def test_conflicting_card_and_flag_cwd_refused(service, checkouts, capsys):
    caller, reviewed = checkouts
    assert cli.main(['spawn', 'Review', '--role', 'review', '--writer-family', 'gpt',
        '--card', json.dumps({'estimate_tokens': 1000, 'cwd': str(caller)}),
        '--cwd', str(reviewed), '--dry-run']) == 2
    assert 'cwd' in capsys.readouterr().err and not service.runner.jobs


def test_card_cwd_schema():
    assert mcp.CARD_SCHEMA['properties']['cwd']['type'] == 'string'


@pytest.mark.parametrize('value', [None, '', 7, [], {}])
def test_card_cwd_requires_path_string(value):
    from plangraph.routing_errors import ParameterRefused
    from plangraph.spawner.prompt_input import resolve_cwd
    with pytest.raises(ParameterRefused, match='cwd.*absolute'):
        resolve_cwd(None, {'cwd': value})


def test_overflow_keeps_explicit_cwd(checkouts, tmp_path, monkeypatch):
    from unittest.mock import Mock
    from plangraph.spawner import overflow
    caller, reviewed = checkouts
    config = tmp_path / 'overflow.json'
    config.write_text(json.dumps({'public_repos': [str(reviewed)], 'hosts': {'remote': {}}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 0))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {'remote': dict(
        ssh='remote', ok=True, fresh=True, live_runs=0,
        mem_available_mb=100000, role_mb=2048, floor_mb=3000)})
    remote = Mock(return_value={'run_id': 'run-remote', 'status': 'running'})
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    service = spawn_service(SpawnerRunner(default_cwd=str(caller)), state=tmp_path / 'state')
    assert service.admit_spawn({'cwd': str(reviewed)}, card={})['overflow_remote']
    assert remote.call_args.args[-1] == ['--cwd', str(reviewed)]
