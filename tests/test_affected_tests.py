import importlib.util
from pathlib import Path

import pytest


def selector():
    spec = importlib.util.spec_from_file_location(
        "affected_tests", Path(__file__).resolve().parents[1] / "tools/affected_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("changed,expected", [
    (["src/pkg/core.py"], ["tests/test_api.py"]),
    (["tests/test_api.py"], ["tests/test_api.py"]),
    (["tests/conftest.py"], ["tests"]),
    (["pyproject.toml"], ["tests"]),
    ([".github/workflows/check.yml"], ["tests"]),
    (["src/pkg/__init__.py"], ["tests/test_api.py"]),
    (["unknown.txt"], ["tests"]),
    (["tests/helper.py"], ["tests"]),
    (["src/pkg/orphan.py"], ["tests"]),
    ([], []),
])
def test_selection(tmp_path, changed, expected):
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "src/pkg/api.py": "from .core import VALUE",
        "src/pkg/orphan.py": "",
        "tests/test_api.py": "from pkg import api",
        "tests/test_other.py": "import os",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert selector().select_tests(tmp_path, changed)["tests"] == expected


def test_changed_files_includes_worktree_and_untracked(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "tracked.py").write_text("before")
    git("add", ".")
    git("commit", "-qm", "base")
    (tmp_path / "tracked.py").write_text("after")
    (tmp_path / "new.py").write_text("")
    assert selector().changed_files(tmp_path, "HEAD") == ["new.py", "tracked.py"]
    with pytest.raises(subprocess.CalledProcessError):
        selector().changed_files(tmp_path, "no-such-ref")
