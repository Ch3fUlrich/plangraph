from __future__ import annotations

import fnmatch
import subprocess

import pytest
from pathlib import Path

from test_ci_commit_gate import REPO_ROOT, _commit, _head, check, ci_commit_gate, repo


def test_seat_record_is_auth_door_path(repo: Path):
    assert any(
        fnmatch.fnmatchcase('tools/seat_record.py', pattern)
        for pattern in ci_commit_gate.AUTH_DOOR_PATHS
    )
    base = _head(repo)
    sha = _commit(
        repo,
        'fix: tweak\n\nHand-Edit: typo in docs\n',
        files={'tools/seat_record.py': 'x'},
    )
    assert check(base, sha, cwd=repo) == [
        f'{sha}: hand-edit touches auth/door surface: tools/seat_record.py'
    ]


@pytest.mark.parametrize('source', ['part', 'event'])
def test_item_trailer_warning_routes_part_and_pr_text_to_rust(tmp_path, monkeypatch, capsys, source):
    import json
    text = 'Fix plangraph:LE-A and plangraph:LE-B'
    path = tmp_path / 'part.txt'
    path.write_text(text)
    monkeypatch.delenv('GITHUB_EVENT_PATH', raising=False)
    if source == 'event':
        path.write_text(json.dumps({'pull_request': {'title': text, 'body': None, 'head': {'sha': 'head'}}}))
        monkeypatch.setenv('GITHUB_EVENT_PATH', str(path))
    monkeypatch.setattr(ci_commit_gate, '_git_text', lambda args: 'head' if '--verify' in args else str(REPO_ROOT))

    def run(argv, **kwargs):
        assert argv[-3:] == ['--plan-item-trailers', 'base', 'head']
        assert kwargs['input'].strip() == text
        return subprocess.CompletedProcess(argv, 0, 'warning: plangraph:LE-B\n', '')

    monkeypatch.setattr(ci_commit_gate.subprocess, 'run', run)
    ci_commit_gate.warn_item_trailers('base', 'head', str(path) if source == 'part' else None)
    assert 'warning: plangraph:LE-B' in capsys.readouterr().err
