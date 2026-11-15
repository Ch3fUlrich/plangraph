"""SPAWNER-SEAT-CWD: local Git checkouts and fake transport, never live seats."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner import cli, mcp, notify, resolver, seat_spawn
from plangraph.spawner.prompt_input import checkout_env
from plangraph.spawner.runner import JobInfo, SpawnerRunner, spawn_service
from _spawner_git_redirect import git_redirect  # noqa: F401 shared redirect fixture


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL, env=checkout_env()).strip()


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
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
@pytest.mark.parametrize('bad', ['relative', 'missing', 'non-git', 'file', 'bare', 'nested', 'unborn'])
def test_invalid_cwd_refused(service, tmp_path, checkouts, capsys, source, bad):
    path = (checkouts[1] if bad == 'nested' else tmp_path) / bad
    if bad == 'non-git':
        path.mkdir()
    elif bad == 'file':
        path.write_text('not a directory')
    elif bad == 'bare':
        git(tmp_path, 'init', '--bare', '-q', str(path))
    elif bad in ('nested', 'unborn'):
        path.mkdir()
        if bad == 'unborn':
            git(path, 'init', '-q', '--template=')
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


@pytest.mark.parametrize('source', ['flag', 'card', 'both', 'equals'])
@pytest.mark.parametrize('different_diff', [False, True])
def test_overflow_keeps_explicit_cwd(checkouts, tmp_path, monkeypatch, source, different_diff):
    from plangraph.spawner import overflow
    caller, reviewed = checkouts
    host = dict(ssh='invented', repo='/invented checkout', spawner_src='/invented/src')
    config = tmp_path / 'overflow.json'
    config.write_text(json.dumps({'public_repos': [str(reviewed)], 'hosts': {'remote': host}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 0))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {'remote': dict(
        ssh='remote', ok=True, fresh=True, live_runs=0,
        mem_available_mb=100000, role_mb=2048, floor_mb=3000)})
    monkeypatch.setattr(overflow.subprocess, 'run', lambda *a, **kw: pytest.fail('explicit cwd left host'))
    if source != 'equals':
        monkeypatch.setattr(overflow, 'remote_spawn', lambda *a: pytest.fail('explicit cwd offered remotely'))
    service = spawn_service(SpawnerRunner(default_cwd=str(caller)), state=tmp_path / 'state')
    card = {'cwd': str(reviewed)} if source in ('card', 'both') else {}
    if different_diff:
        card['diff_ref'] = dict(repo=str(caller), base='abc', head='def')
    original = json.loads(json.dumps(card))
    if source == 'equals':
        with pytest.raises(overflow.RemoteRefusal, match='explicit cwd'):
            overflow.remote_spawn('remote', host, card, 'Review', ['--cwd=' + str(reviewed)])
    else:
        result = service.admit_spawn({'cwd': str(reviewed)} if source != 'card' else {}, card=card)
        assert result['status'] == 'queued' and result['exit_code'] == 5
        assert not result.get('overflow_remote')
        assert service.result(result['run_id']) == result
    assert card == original


def test_cwd_validation_ignores_git_redirects(checkouts, tmp_path, monkeypatch, git_redirect):
    from plangraph.spawner.prompt_input import resolve_cwd
    from plangraph.routing_errors import ParameterRefused
    caller, reviewed = checkouts
    assert resolve_cwd(str(reviewed), {}) == str(reviewed)
    with pytest.raises(ParameterRefused, match='git checkout'):
        resolve_cwd(str(tmp_path), {})


def test_completion_ignores_git_redirects(checkouts, monkeypatch, git_redirect):
    caller, reviewed = checkouts
    expected = git(reviewed, 'rev-parse', 'HEAD')
    monkeypatch.setenv('GIT_DIR', str(caller / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(caller))
    event = notify.completion_event(dict(run_id='run-test', status='completed', exit_code=0), reviewed)
    assert event['head_sha'] == expected


def test_watchdog_head_ignores_git_redirects(checkouts, monkeypatch, git_redirect):
    from plangraph.spawner.run_watchdog import edit_signature
    caller, reviewed = checkouts
    expected = git(reviewed, 'rev-parse', 'HEAD').encode()
    monkeypatch.setenv('GIT_DIR', str(caller / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(caller))
    assert edit_signature(reviewed)[1].strip() == expected


def test_worktree_git_ignores_redirects(checkouts, tmp_path, monkeypatch, git_redirect):
    from plangraph.spawner import git_guard
    caller, reviewed = checkouts
    git(reviewed, 'branch', '-M', 'main')
    git(reviewed, 'remote', 'add', 'origin', str(reviewed))
    expected = git(reviewed, 'rev-parse', 'HEAD')
    monkeypatch.setenv('GIT_DIR', str(caller / '.git'))
    assert git_guard.init_guard(reviewed)[0] == reviewed / '.git' / 'config'
    metadata = git_guard.create(tmp_path / 'worktrees', reviewed, 'lane/redirect')
    assert metadata['base_sha'] == expected


def test_cwd_validation_off_event_loop(service, checkouts, monkeypatch):
    from plangraph.spawner import prompt_input
    resolve = prompt_input.resolve_cwd
    loop_thread = threading.get_ident()
    def checked(*args):
        assert threading.get_ident() != loop_thread
        return resolve(*args)
    monkeypatch.setattr(prompt_input, 'resolve_cwd', checked)
    asyncio.run(service.spawn(card={'estimate_tokens': 1000}, cwd=str(checkouts[1]),
        prompt='Review', role='review', writer_families=['gpt'], dry_run=True))


@pytest.mark.parametrize('source', ['flag', 'card'])
@pytest.mark.parametrize('legacy', [False, True])
def test_run_worktree_uses_selected_checkout(service, checkouts, capsys, monkeypatch, source, legacy):
    caller, reviewed = checkouts
    git(reviewed, 'branch', '-M', 'main')
    git(reviewed, 'remote', 'add', 'origin', str(reviewed))
    card = {'estimate_tokens': 1000, 'diff_ref': dict(repo=str(reviewed),
        base=git(reviewed, 'rev-parse', 'HEAD~1'), head=git(reviewed, 'rev-parse', 'HEAD')),
        **({'cwd': str(reviewed)} if source == 'card' else {})}
    assert cli.main(['spawn', 'Review', '--role', 'review', '--writer-family', 'gpt',
        '--card', json.dumps(card), '--run-worktree',
        *(['--cwd', str(reviewed)] if source == 'flag' else [])]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record['status'] == 'completed', record.get('error')
    assert service.launch_cwds == [record['cwd']] != [str(caller)]
    assert git(record['cwd'], 'rev-parse', '--git-common-dir') == str(reviewed / '.git')
    assert record['completion']['head_sha'] == git(reviewed, 'rev-parse', 'HEAD')
    async def spawn(**kwargs):
        return JobInfo('fixer', 'completed', kwargs['prompt'], 'test', exit_code=0)
    monkeypatch.setattr(service.runner, 'spawn', spawn)
    record['role'] = 'implement'
    git(reviewed, 'worktree', 'remove', record['cwd'])
    assert not Path(record['cwd']).exists()
    if legacy:
        record.pop('selected_cwd')
        service.runner.default_cwd = str(reviewed)  # The pre-upgrade service's checkout.
    asyncio.run(service._continue_writer(record, 'Remaining gaps: finish the test'))
    child = service.result(record['continuation_run_id'])
    assert child['status'] == 'completed', child.get('error')
    assert child['cwd'] != record['cwd']
    assert record['selected_cwd'] == child['selected_cwd'] == str(reviewed)
    assert child['run_worktree']['base_sha'] == record['completion']['head_sha']


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('source', ['flag', 'card', 'both'])
@pytest.mark.parametrize('spelling', ['symlink', 'parent', 'trailing'])
def test_cli_top_level_spacing(service, checkouts, tmp_path, monkeypatch, capsys, dry_run, source, spelling):
    """Guard CLI behavior rather than source formatting (legacy test name)."""
    reviewed = checkouts[1]
    alias = tmp_path / 'alias'
    alias.symlink_to(reviewed, target_is_directory=True)
    value = {'symlink': str(alias), 'parent': str(reviewed / '..' / reviewed.name),
             'trailing': str(reviewed) + '/'}[spelling]
    validations, output = [], subprocess.check_output
    def checked(command, **kwargs):
        if '--show-toplevel' in command:
            validations.append(command)
        return output(command, **kwargs)
    monkeypatch.setattr(subprocess, 'check_output', checked)
    card = dict(estimate_tokens=1000, diff_ref=dict(repo=str(reviewed),
        base=git(reviewed, 'rev-parse', 'HEAD~1'), head=git(reviewed, 'rev-parse', 'HEAD')),
        **({'cwd': value} if source != 'flag' else {}))
    assert cli.main(['spawn', 'Review', '--role', 'review', '--writer-family', 'gpt',
        '--card', json.dumps(card), *(['--cwd', value] if source != 'card' else []),
        *(['--dry-run'] if dry_run else [])]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record['cwd'] == str(reviewed) and len(validations) == 1
    assert dry_run or service.launch_cwds == [str(reviewed)]


def test_admission_off_event_loop(service, checkouts, monkeypatch):
    loop_thread = threading.get_ident()
    def checked(*args, **kwargs):
        assert threading.get_ident() != loop_thread
    monkeypatch.setattr(service, 'admit_spawn', checked)
    asyncio.run(service.spawn(card={'estimate_tokens': 1000}, cwd=str(checkouts[1]),
        prompt='Review', role='review', writer_families=['gpt'], dry_run=True))


@pytest.mark.parametrize('source', ['flag', 'card'])
def test_legacy_overflow_classifies_explicit_cwd(checkouts, tmp_path, monkeypatch, capsys, source):
    from plangraph.spawner import overflow
    caller, reviewed = checkouts
    config, card, prompt = [tmp_path / name for name in ('overflow.json', 'card.json', 'prompt.txt')]
    config.write_text(json.dumps({'public_repos': [str(caller)], 'hosts': {}}))
    card.write_text(json.dumps({'cwd': str(reviewed)} if source == 'card' else {}))
    prompt.write_text('Review')
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'local_resources', lambda: (0, 0))
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {})
    assert overflow.main(['spawn', '--repo', str(caller), '--card', str(card), '--prompt', str(prompt),
        *(['--cwd', str(reviewed)] if source == 'flag' else [])]) == 2
    assert 'repo is not public' in capsys.readouterr().out
