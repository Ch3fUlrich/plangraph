from __future__ import annotations

import fnmatch
from pathlib import Path

from test_ci_commit_gate import _commit, _head, check, ci_commit_gate, repo


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
