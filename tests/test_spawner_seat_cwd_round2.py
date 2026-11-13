"""Checkout-bound overflow refuses before transport; both repository gates stay closed."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner import overflow, seat_spawn
from test_spawner_seat_cwd import checkouts  # noqa: F401 shared local Git fixture


@pytest.mark.parametrize('source', ['flag', 'card', 'both', 'equals'])
def test_remote_explicit_cwd_refused_before_transport(monkeypatch, source):
    card = {'cwd': '/invented-selected'} if source in ('card', 'both') else {}
    options = ([] if source == 'card' else ['--cwd=/invented-selected'] if source == 'equals'
               else ['--cwd', '/invented-selected'])
    monkeypatch.setattr(overflow.subprocess, 'run', lambda *a, **kw: pytest.fail('transport started'))
    with pytest.raises(overflow.RemoteRefusal, match='explicit cwd'):
        overflow.remote_spawn('remote', {'ssh': 'invented'}, card, 'Review', options)


@pytest.mark.parametrize('entry', ['main', 'admit'])
@pytest.mark.parametrize('source', ['flag', 'card'])
@pytest.mark.parametrize('diff_public', [False, True])
@pytest.mark.parametrize('selected_public', [False, True])
def test_overflow_checks_selected_and_diff_repositories(checkouts, tmp_path, monkeypatch, capsys,
                                                       entry, source, diff_public, selected_public):
    caller, reviewed = checkouts
    config, card_path, prompt = [tmp_path / name for name in ('overflow.json', 'card.json', 'prompt.txt')]
    config.write_text(json.dumps({'public_repos': [*([str(reviewed)] if selected_public else []),
                                                  *([str(caller)] if diff_public else [])],
                                  'hosts': {'remote': {}}}))
    card = {'diff_ref': dict(repo=str(caller), base='abc', head='def'),
            **({'cwd': str(reviewed)} if source == 'card' else {})}
    card_path.write_text(json.dumps(card))
    prompt.write_text('Review')
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'local_limits', lambda *a: (2048, 3000))
    monkeypatch.setattr(overflow, 'local_resources', lambda: (0, 0))
    monkeypatch.setattr(seat_spawn, 'resources', lambda *a: (0, 0))
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {})
    seen, decide = [], overflow.decide
    def checked(memory, role, floor, records, public, **kwargs):
        seen.append(public)
        return decide(memory, role, floor, records, public, **kwargs)
    monkeypatch.setattr(overflow, 'decide', checked)
    if entry == 'main':
        assert overflow.main(['spawn', '--repo', str(caller), '--card', str(card_path),
            '--prompt', str(prompt), *(['--cwd', str(reviewed)] if source == 'flag' else [])]) == 2
        if not (selected_public and diff_public):
            assert 'not public' in capsys.readouterr().out
    else:
        service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(caller)))
        result = seat_spawn.admit(service, {'cwd': str(reviewed)} if source == 'flag' else {}, card=card)
        assert result['status'] == 'queued'
        if not (selected_public and diff_public):
            assert 'not public' in result['reason']
    assert seen[-1] is (selected_public and diff_public)


@pytest.mark.parametrize('entry', ['main', 'admit'])
@pytest.mark.parametrize('remote_first', [False, True])
def test_explicit_checkout_never_uses_healthy_remote(checkouts, tmp_path, monkeypatch,
                                                   entry, remote_first):
    caller, reviewed = checkouts
    config, card, prompt = [tmp_path / name for name in ('overflow.json', 'card.json', 'prompt.txt')]
    config.write_text(json.dumps({'public_repos': [str(reviewed)],
                                  'hosts': {'remote': {'ssh': 'invented'}}}))
    card.write_text(json.dumps({'cwd': str(reviewed)}))
    prompt.write_text('Review')
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'placement_policy', lambda *a: ('local', remote_first))
    monkeypatch.setattr(overflow, 'local_limits', lambda *a: (2048, 3000))
    monkeypatch.setattr(overflow, 'local_health_refusal', lambda *a: 'memory')
    monkeypatch.setattr(overflow, 'local_resources', lambda: (0, 0))
    monkeypatch.setattr(seat_spawn, 'resources', lambda *a: (0, 0))
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {'remote': dict(
        ssh='invented', ok=True, fresh=True, live_runs=0,
        mem_available_mb=100000, role_mb=2048, floor_mb=3000)})
    run = overflow.subprocess.run
    def local_only(argv, **kwargs):
        assert argv[0] not in ('ssh', 'scp'), 'explicit checkout left host'
        return run(argv, **kwargs)
    monkeypatch.setattr(overflow.subprocess, 'run', local_only)
    if entry == 'main':
        if remote_first:
            def local(argv):
                assert argv[argv.index('--card') + 1] == '@' + str(card)
                return 5
            monkeypatch.setattr(overflow.cli, 'main', local)
        assert overflow.main(['spawn', '--repo', str(caller), '--card', str(card),
                              '--prompt', str(prompt)]) == (5 if remote_first else 2)
    else:
        service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(caller)))
        result = seat_spawn.admit(service, {}, card={'cwd': str(reviewed)})
        assert result['status'] == ('refused' if remote_first else 'queued')
        assert not result.get('overflow_remote')
        assert ('memory' if remote_first else 'explicit cwd') in result['reason']


def test_worktree_attempts_keep_original_checkout(checkouts, tmp_path, monkeypatch):
    from plangraph.spawner import git_guard
    caller, reviewed = checkouts
    calls = []
    def create(root, repo, branch, continuation, **kwargs):
        calls.append((repo, continuation))
        return dict(path=str(tmp_path / ('attempt-' + str(len(calls)))), branch=branch, base_sha='abc')
    monkeypatch.setattr(git_guard, 'create', create)
    service = SimpleNamespace(state=tmp_path / 'state', runner=SimpleNamespace(default_cwd=str(caller)),
                              _save=lambda record: None)
    record = dict(run_id='run-test', cwd=str(reviewed))
    async def run():
        await git_guard.prepare_worktree(service, record, {}, True)
        await git_guard.prepare_worktree(service, record, {}, True)
    asyncio.run(run())
    assert calls == [(str(reviewed), False), (str(reviewed), True)]
    assert record['selected_cwd'] == str(reviewed)
    assert len(record['worktree_attempts']) == 2
