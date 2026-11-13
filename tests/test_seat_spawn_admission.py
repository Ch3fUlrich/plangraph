"""Admission contracts for changelog.d/SEAT-RECORDS-ALWAYS-POSTABLE-1.md."""
import asyncio
import json
import os
import socket
import subprocess

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan, SpawnRefused
from plangraph.spawner import cli
from plangraph.spawner.item_key import _git, admit_seat, annotate_item
from plangraph.spawner.mcp import MCPSpawnerServer
from plangraph.spawner.notify import FileInbox
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.service import SpawnService
from registry_route_util import _Runner
KEY = 'demo:SEAT-RECORDS-ALWAYS-POSTABLE'
MISSING = 'seat_item_missing: card needs item_key (or item)'

@pytest.fixture
def seat_setup(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **kw: pytest.fail('network'))
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], env=env,
                              check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('commit', '--allow-empty', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    return repo, base, git

@pytest.mark.parametrize('entry', ['base', 'dispatch', 'cli', 'mcp'])
@pytest.mark.parametrize('case,role,error,expected', [
    ('missing', 'review', MISSING, None),
    ('missing', 'judge', MISSING, None),
    ('invalid', 'review', MISSING, None),
    ('trailer', 'review', None, KEY),
    ('trailer', 'judge', None, KEY),
    ('invalid_with_trailer', 'review', None, KEY),
    ('body_only', 'review', MISSING, None),
    ('bad_trailer', 'review', MISSING, None),
    ('conflicting_trailers', 'review', MISSING, None),
    ('alias', 'review', None, KEY),
    ('explicit', 'review', None, KEY),
    ('environment', 'review', None, KEY),
    ('equal', 'review', 'empty_diff: base equals head', KEY),
    ('equal', 'judge', 'empty_diff: base equals head', KEY),
    ('equal_without_item', 'review', 'empty_diff: base equals head', None),
    ('equal_without_item', 'judge', 'empty_diff: base equals head', None),
    ('different', 'review', None, KEY),
    ('different', 'judge', None, KEY),
    ('unresolved', 'review', 'diff_ref git lookup failed', KEY),
    ('unresolved', 'judge', 'diff_ref git lookup failed', KEY),
    ('unresolved_without_item', 'review', 'diff_ref git lookup failed', None),
    ('unresolved_without_item', 'judge', 'diff_ref git lookup failed', None),
    ('unresolved_head_without_item', 'review', 'diff_ref git lookup failed', None),
    ('unresolved_head_without_item', 'judge', 'diff_ref git lookup failed', None),
    ('missing_repo', 'review', 'diff_ref git lookup failed', KEY),
    ('missing_repo', 'judge', 'diff_ref git lookup failed', KEY),
    ('missing_repo_without_item', 'review', 'diff_ref git lookup failed', None),
    ('missing_repo_without_item', 'judge', 'diff_ref git lookup failed', None),
    ('trailer', 'implement', None, None),
    ('equal', 'implement', None, KEY),
])
def test_seat_spawn_prelaunch_identity_and_diff(
        seat_setup, tmp_path, monkeypatch, capsys, entry, case, role, error, expected):
    repo, base, git = seat_setup
    message = ('head\n\nPlan-Item: ' + KEY if case in ('trailer', 'invalid_with_trailer') else
               'head\n\nPlan-Item: demo:OTHER' if case == 'explicit' else
               'head\n\nPlan-Item: ' + KEY + '\nPlan-Item: demo:OTHER' if case == 'conflicting_trailers' else
               'head\n\nPlan-Item: invalid' if case == 'bad_trailer' else
               'head\n\nPlan-Item: ' + KEY + '\n\nNot a trailer.' if case == 'body_only' else 'head')
    (repo / 'change.txt').write_text('change\n')
    git('add', 'change.txt')
    git('commit', '-qm', message)
    head = git('rev-parse', 'HEAD')
    git('branch', 'review-head', head); git('checkout', '-q', '--detach', base)  # named head, not cwd/HEAD
    card = dict(estimate_tokens=100, diff_ref=dict(repo=str(repo), base=base, head='review-head'))
    if case in ('invalid', 'invalid_with_trailer'):
        card['item_key'] = 'invalid'
    if case == 'alias': card['item'] = KEY
    if case == 'environment':
        monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', KEY)
    if case in ('equal', 'different', 'unresolved', 'explicit', 'missing_repo'):
        card['item_key'] = KEY
    if case in ('equal', 'equal_without_item'):
        card['diff_ref']['base'] = head[:12]  # distinct spellings resolve to the same commit
    if case in ('unresolved', 'unresolved_without_item'):
        card['diff_ref']['base'] = 'absent-ref'
    if case == 'unresolved_head_without_item':
        card['diff_ref']['head'] = 'absent-ref'
    if case in ('missing_repo', 'missing_repo_without_item'):
        card['diff_ref']['repo'] = str(tmp_path / 'absent-repo')
    runner = _Runner()
    state = tmp_path / 'state'
    service = (SpawnService(runner, state=state) if entry == 'base' else
               spawn_service(runner, state=state, keeper_sink=FileInbox(state)))
    plan = SpawnPlan((DEFAULT_LEGS[0],), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    admissions = []
    monkeypatch.setattr(service, 'admit_spawn', lambda *a, **kw: admissions.append(True))
    async def prepare(prompt, *args, **kwargs):
        return prompt
    monkeypatch.setattr(service, '_prepare_leg_prompt', prepare)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service); monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **kw: pytest.fail('supervisor'))
    options = dict(card=card, prompt='Review the change', role=role,
                   writer_families=['google'], dry_run=True, wait_seconds=None)
    if entry == 'cli':
        args = ['spawn', options['prompt'], '--card', json.dumps(card), '--role', role,
                '--writer-family', 'google'] + ([] if error else ['--dry-run'])
        code = cli.main(args)
        output = capsys.readouterr()
        assert code == (2 if error else 0), output
        if error:
            assert error in output.err
        else:
            result = json.loads(output.out)
    elif entry == 'mcp':
        server = MCPSpawnerServer(runner, state=state)
        server.service = service
        response = asyncio.run(server.handle_rpc_request(dict(jsonrpc='2.0', id=1,
            method='tools/call', params=dict(name='plangraph_agent_spawn', arguments=options))))
        if error:
            assert error in response['error']['message']
        else:
            result = json.loads(response['result']['content'][0]['text'])
    elif error:
        with pytest.raises(SpawnRefused, match=error.replace('(', r'\(').replace(')', r'\)')):
            asyncio.run(service.spawn(**options))
    else:
        result = asyncio.run(service.spawn(**options))
    if error:
        assert runner.calls == []
        assert admissions == []
        refused, = service.ps()['runs']
        assert refused['status'] == 'refused' and error in refused['error']
        if 'unresolved' in case or 'missing_repo' in case:
            assert 'exit=128' in refused['error'] and 'fatal:' in refused['error']
        if role == 'judge':
            assert refused['role'] == 'review'
    else:
        assert result['status'] == 'dry_run'
        if role == 'judge':
            assert result['role'] == 'review'
        assert result['item_key'] == expected
        assert service.result(result['run_id'])['item_key'] == expected
        assert len(runner.calls) == 1
        if case == 'invalid_with_trailer':
            assert 'invalid item_key; replaced from Plan-Item trailer' in result['warnings']


@pytest.mark.parametrize('field', ['item_key', 'item'])
def test_trailer_recovery_preserves_invalid_identity_warning(seat_setup, field):
    repo, base, git = seat_setup
    git('commit', '--allow-empty', '-qm', 'head\n\nPlan-Item: ' + KEY)
    card = {field: 'invalid', 'diff_ref': dict(repo=str(repo), base=base, head='HEAD')}
    original = dict(card)
    admitted = admit_seat(card, 'review')
    record = {'role': 'review'}
    annotate_item(record, admitted)
    assert card == original and admitted['item_key'] == record['item_key'] == KEY
    assert record['warnings'] == ['invalid item_key; replaced from Plan-Item trailer']


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'oserror'])
def test_admission_git_failure_detail_is_redacted(monkeypatch, failure):
    from plangraph.spawner import item_key
    secret, token = 'test-secret', 'ghp_' + 'a' * 36
    monkeypatch.setenv('TEST_GIT_SECRET', secret)
    stderr = ('noise\n' * 300 + secret + ' ' + token + '\nfatal: lookup detail').encode()
    def fail(*args, **kwargs):
        assert kwargs['timeout'] == 10
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['git', secret], 10, stderr=stderr)
        if failure == 'oserror':
            raise OSError(2, secret)
        return subprocess.CompletedProcess(args[0], 128, '', stderr.decode())
    monkeypatch.setattr(item_key.subprocess, 'run', fail)
    with pytest.raises(SpawnRefused) as refused:
        _git({'repo': '/repo'}, 'rev-parse', 'HEAD')
    detail = str(refused.value)
    assert detail.startswith('diff_ref git lookup failed') and len(detail) < 400
    assert secret not in detail and token not in detail
    if failure == 'oserror':
        assert 'oserror errno=2' in detail
    else:
        assert ('exit=128' if failure == 'exit' else 'timeout=10s') in detail
        assert 'fatal: lookup detail' in detail and '[REDACTED]' in detail
