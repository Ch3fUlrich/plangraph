"""Persistent checkout alarm transitions use the existing keeper alert handler."""
import json
import subprocess
import sys

import pytest

from plangraph.fleet_sync import drift
from plangraph.fleet_sync.__main__ import main


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo / 'tracked').write_text('initial\n')
    git('add', '.')
    git('commit', '-m', 'one')
    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
    git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    return repo, git


def test_transitions_and_hourly_reminders(checkout, tmp_path):
    repo, git = checkout
    state = tmp_path / 'state'
    sent = []
    def tick(now):
        return drift.check_checkout(repo, sent.append, state_dir=state, now=now)
    assert not tick(0) and not sent
    git('checkout', '--detach')
    assert tick(1)
    assert len(sent) == 1 and 'alarm' in sent[-1]
    assert tick(300) and tick(3600)
    assert len(sent) == 1
    assert tick(3601) and 'reminder' in sent[-1]
    assert len(sent) == 2
    git('checkout', 'main')
    assert not tick(3602) and 'clear' in sent[-1]
    assert not tick(7202) and len(sent) == 3
    git('checkout', '--detach')
    assert tick(7203) and len(sent) == 4


def test_failed_delivery_retries_without_checkpoint(checkout, tmp_path):
    repo, git = checkout
    git('checkout', '--detach')
    def fail(_line):
        raise OSError('fixture alarm unavailable')
    with pytest.raises(OSError):
        drift.check_checkout(repo, fail, state_dir=tmp_path / 'state', now=0)
    sent = []
    drift.check_checkout(repo, sent.append, state_dir=tmp_path / 'state', now=1)
    assert len(sent) == 1


def test_real_handler_and_missing_handler_exit_nonzero(checkout, tmp_path, monkeypatch):
    repo, git = checkout
    output = tmp_path / 'delivered.jsonl'
    handler = tmp_path / 'alert.py'
    handler.write_text('import json,sys\nfrom pathlib import Path\n'
                       f'with Path({str(output)!r}).open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    monkeypatch.setenv('PLANGRAPH_ALERT_CMD', f'{sys.executable} {handler} fixed-argument')
    args = ['check-checkout', '--repo', str(repo), '--state-dir', str(tmp_path / 'state')]
    git('checkout', '--detach')
    assert main(args) == 1
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0][0] == 'fixed-argument' and rows[0][-1].startswith('production-checkout alarm:')
    assert main(args) == 1
    assert len(output.read_text().splitlines()) == 1
    git('checkout', 'main')
    assert main(args) == 0
    assert 'clear' in json.loads(output.read_text().splitlines()[-1])[-1]
    monkeypatch.delenv('PLANGRAPH_ALERT_CMD')
    assert main(args) != 0
    monkeypatch.setenv('PLANGRAPH_ALERT_CMD', str(tmp_path / 'missing-handler'))
    assert main(args) != 0


@pytest.mark.parametrize('condition', ['ahead', 'diverged', 'skip-worktree', 'assume-unchanged'])
def test_hidden_or_unmerged_production_code_alarms(checkout, tmp_path, condition):
    repo, git = checkout
    if condition in {'ahead', 'diverged'}:
        if condition == 'diverged':
            git('checkout', '-b', 'remote')
            git('commit', '--allow-empty', '-m', 'remote')
            git('update-ref', 'refs/remotes/origin/main', 'HEAD')
            git('checkout', 'main')
        git('commit', '--allow-empty', '-m', 'unmerged local')
    else:
        git('update-index', '--' + condition, 'tracked')
        (repo / 'tracked').write_text('hidden change\n')
    sent = []
    assert drift.check_checkout(repo, sent.append, state_dir=tmp_path / 'state')
    assert len(sent) == 1
    assert ('unmerged' if condition in {'ahead', 'diverged'} else 'hidden') in sent[0]


@pytest.mark.parametrize('broken', [
    '{', '', '[]', '{"bad":1,"sent":0}', '{"bad":true,"sent":"old"}',
    '{"bad":true,"sent":NaN}',
    pytest.param("[" * 1500 + "]" * 1500, id="deep-list"),
])
def test_corrupt_alarm_state_still_alarms_and_is_replaced(checkout, tmp_path, monkeypatch, broken):
    import hashlib
    import os
    from pathlib import Path

    repo, git = checkout
    git('checkout', '--detach')
    state = tmp_path / 'state'
    state.mkdir()
    path = state / (hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16] + '.json')
    path.write_text(broken)
    replace = os.replace
    replacements = []
    def atomic_replace(source, destination):
        source = Path(source)
        assert destination == path and source.parent == path.parent
        assert path.read_text() == broken, 'old state must remain intact until rename'
        assert json.loads(source.read_text()) == {'bad': True, 'sent': 100}
        replacements.append(destination)
        replace(source, destination)
    monkeypatch.setattr(os, 'replace', atomic_replace)
    sent = []
    assert drift.check_checkout(repo, sent.append, state_dir=state, now=100)
    assert len(sent) == 1 and sent[0].startswith('production-checkout alarm:')
    assert replacements == [path]
    assert json.loads(path.read_text()) == {'bad': True, 'sent': 100}
    drift.check_checkout(repo, sent.append, state_dir=state, now=101)
    assert len(sent) == 1, 'repaired state must resume normal dedupe'
