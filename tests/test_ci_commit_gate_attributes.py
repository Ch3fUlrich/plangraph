"""A merge attribute is a gate path (LAND-PART-ENTRY-CHECKS)."""
from pathlib import Path

import pytest

from test_ci_commit_gate import OK_MSG, _commit, _head, check, repo  # noqa: F401


@pytest.mark.parametrize("path", [".gitattributes", "tools/.gitattributes", "rust/pg-spawnd/tests/.gitattributes"])
def test_gitattributes_at_any_depth_is_a_gate_path(repo: Path, path: str):
    """A merge attribute changes what a merge produces, so a part or a resolution that edits one needs a grant."""
    base = _head(repo)
    head = _commit(repo, OK_MSG, files={path: "*.lock merge=union\n"})
    assert check(base, head, cwd=repo) == [f"{head}: gate edit without Gate-Edit trailer"]
    granted = _commit(repo, "chore: attrs\n\nWriter: mimo-v2.6-flash run-g\n\nGate-Edit: D-6530\n",
                      files={path: "*.lock merge=binary\n"})
    assert check(base, granted, cwd=repo) == [f"{head}: gate edit without Gate-Edit trailer"]
