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
    (["unknown.txt"], []),
    (["docs/guide.md", "status/card", "build/generated.py", "scratch.py"], []),
    (["tests/notes.md", "src/pkg/README.md"], []),
    (["src/pkg/missing.py"], ["tests"]),
    (["tools/new_tool.py"], ["tests"]),
    (["out/generated.py", "docs/example.py", "status/card.md"], []),
    (["docs/guide.md", "src/pkg/core.py", "status/card"], ["tests/test_api.py"]),
    (["conftest.py"], ["tests"]),
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
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build/generated.py").write_text("")
    (tmp_path / "notes.md").write_text("")
    changed = selector().changed_files(tmp_path, "HEAD")
    assert changed == [".gitignore", "new.py", "notes.md", "tracked.py"]
    assert selector().select_tests(tmp_path, changed)["tests"] == []
    with pytest.raises(subprocess.CalledProcessError):
        selector().changed_files(tmp_path, "no-such-ref")


def _asset_repo(tmp_path, extra=None):
    """A tracked source tree whose assets are non-Python files under src/."""
    import subprocess

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "src/pkg/api.py": "from .core import VALUE",
        "src/pkg/asset.sql": "CREATE TABLE asset(id);",
        "tests/test_api.py": "from pkg import api",
        "tests/test_other.py": "import os",
    }
    files.update(extra or {})
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git("add", ".")
    git("commit", "-qm", "base")
    return git


def test_tracked_non_python_src_asset_falls_back_to_full_suite(tmp_path):
    """A tracked src/*.sql change runs no Python AST, yet production code reads it."""
    _asset_repo(tmp_path)
    plan = selector().select_tests(tmp_path, ["src/pkg/asset.sql"])
    assert plan["tests"] == ["tests"]
    assert plan["reason"].startswith("no mapped tests")


def test_tracked_non_python_src_asset_maps_to_tests_naming_it(tmp_path):
    _asset_repo(tmp_path, {"tests/test_asset.py": 'SQL = "asset.sql"'})
    plan = selector().select_tests(tmp_path, ["src/pkg/asset.sql"])
    assert plan["tests"] == ["tests/test_asset.py"]


def test_untracked_non_python_src_asset_is_skipped(tmp_path):
    """Only ignored/untracked scratch files may be skipped, as before."""
    _asset_repo(tmp_path)
    (tmp_path / "src/pkg/scratch.sql").write_text("-- local scratch\n")
    assert selector().select_tests(tmp_path, ["src/pkg/scratch.sql"])["tests"] == []


def test_ignored_non_python_src_asset_is_skipped(tmp_path):
    _asset_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("src/pkg/generated/\n")
    generated = tmp_path / "src/pkg/generated/gen.sql"
    generated.parent.mkdir()
    generated.write_text("CREATE TABLE gen(id);\n")
    assert selector().select_tests(tmp_path, ["src/pkg/generated/gen.sql"])["tests"] == []


def test_ci_pr_uses_affected_and_nightly_keeps_full():
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1]
                              / ".github/workflows/tests.yml").read_text())
    for job in ("pytest", "pytest-postgres"):
        steps = workflow["jobs"][job]["steps"]
        checkout = next(s for s in steps if s.get("uses") == "actions/checkout@v4")
        assert checkout["with"]["fetch-depth"] == 0
        command = next(s["run"] for s in steps if s.get("name", "").startswith("pytest"))
        assert 'pull_request' in command
        assert 'tools/affected_tests.py --base "$BASE_SHA" --run -n auto' in command
        assert 'python -m pytest -q -n auto' in command
    # PyYAML's YAML 1.1 loader treats the GitHub key `on` as boolean True.
    assert workflow.get("on", workflow.get(True))["schedule"]
