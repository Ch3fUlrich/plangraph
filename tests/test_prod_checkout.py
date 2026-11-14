"""Production checkout alarms and HEAD audit use disposable repositories only."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from plangraph.fleet_sync import drift
from plangraph.fleet_sync.__main__ import main


def git(repo, *args):
    return subprocess.check_output(
        ['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    (root / 'tracked').write_text('first\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'initial')
    git(root, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    return root


@pytest.mark.parametrize('condition', ['detached', 'branch', 'dirty', 'staged', 'clean', 'untracked'])
def test_checkout_alarm(repo, condition):
    if condition == 'detached':
        git(repo, 'checkout', '--detach')
    elif condition == 'branch':
        git(repo, 'checkout', '-b', 'topic')
    elif condition in {'dirty', 'staged'}:
        (repo / 'tracked').write_text('changed\n')
        if condition == 'staged':
            git(repo, 'add', 'tracked')
    elif condition == 'untracked':
        (repo / 'scratch').write_text('ignored by check\n')
    sent = []
    findings = drift.check_checkout(repo, lambda recipient, line: sent.append((recipient, line)))
    assert bool(findings) == (condition in {'detached', 'branch', 'dirty', 'staged'})
    assert len(sent) == bool(findings)
    if findings:
        assert sent[0][0] == 'routing'
        assert 'production-checkout' in sent[0][1]
        assert ('dirty' if condition == 'staged' else condition) in sent[0][1]


def test_missing_checkout_alarms(tmp_path):
    sent = []
    assert drift.check_checkout(tmp_path / 'missing', lambda *args: sent.append(args))
    assert 'unavailable' in sent[0][1]


def test_default_branch_is_not_hardcoded(repo):
    git(repo, 'branch', '-m', 'trunk')
    git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
    assert not drift.check_checkout(repo, lambda *_: pytest.fail('clean default'))


def test_hook_install_dry_run_and_head_moves(repo, tmp_path, monkeypatch):
    state = tmp_path / 'state'
    hook = Path(git(repo, 'rev-parse', '--absolute-git-dir')) / 'hooks' / 'post-checkout'
    assert main(['install-audit', '--repo', str(repo), '--state-dir', str(state)]) == 0
    assert not hook.exists()
    assert not state.exists()
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))
    assert main(['install-audit', '--repo', str(repo), '--state-dir', str(state), '--apply']) == 0
    first = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '-b', 'topic')
    git(repo, 'commit', '--allow-empty', '-m', 'second')
    second = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '--detach', first)
    rows = [json.loads(line) for line in (state / 'head-moves.jsonl').read_text().splitlines()]
    moves = [row for row in rows if row['old_sha'] == second and row['new_sha'] == first]
    assert len(moves) == 1
    assert moves[0]['branch'] == 'detached'
    assert moves[0]['time']
    chain = moves[0]['process_chain']
    assert len(chain) >= 2
    assert any(p['comm'] == 'git' for p in chain)
    assert all(set(p) == {'pid', 'ppid', 'comm'} for p in chain)
    assert any(row['new_sha'] == second for row in rows), 'commit HEAD moves need auditing too'


def test_installer_preserves_existing_hook(repo, tmp_path):
    hook = Path(git(repo, 'rev-parse', '--absolute-git-dir')) / 'hooks' / 'post-checkout'
    hook.write_text('#!/bin/sh\nexit 7\n')
    with pytest.raises(ValueError, match='existing hook'):
        drift.install_audit(repo, tmp_path / 'state', apply=True)
    assert hook.read_text().endswith('exit 7\n')


@pytest.mark.parametrize('detached', [False, True])
def test_spawn_record_has_code_tree_identity(repo, tmp_path, monkeypatch, detached):
    from registry_route_util import _service

    from plangraph.spawner import git_guard

    monkeypatch.setattr(git_guard, 'CODE_TREE', repo, raising=False)
    if detached:
        git(repo, 'checkout', '--detach')
    service = _service(tmp_path, monkeypatch)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='fixture', role='implement', dry_run=True))
    record = service.result(result['run_id'])
    assert record['tree_sha'] == git(repo, 'rev-parse', 'HEAD')
    expected = 'detached' if detached else 'main'
    assert record['tree_branch'] == expected
    git(repo, 'checkout', '-b', 'changed')
    service._save(record)
    assert service.result(result['run_id'])['tree_branch'] == expected, 'spawn identity is immutable'


def test_timer_is_five_minutes():
    root = Path(__file__).resolve().parents[1]
    timer = (root / 'deploy/systemd/plangraph-prod-checkout.timer').read_text()
    assert 'OnUnitActiveSec=5min' in timer
