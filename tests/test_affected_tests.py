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


def test_browser_tests_never_selected(tmp_path):
    # D-2821: tests/browser runs in its own CI job; an explicit path would collect it in pytest.
    for name, text in {"src/pkg/__init__.py": "", "src/pkg/core.py": "VALUE = 1",
                       "tests/test_api.py": "from pkg import core", "tests/browser/test_ui.py": "from pkg import core"}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    assert selector().select_tests(tmp_path, ["src/pkg/core.py"])["tests"] == ["tests/test_api.py"]


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


def test_tracked_deploy_watchdog_maps_to_tests_naming_it(tmp_path):
    _asset_repo(tmp_path, {
        "deploy/watchdog/watchdog.xml": "<watchdog/>",
        "tests/test_watchdog.py": "WATCHDOG = 'deploy/watchdog/watchdog.xml'",
    })
    plan = selector().select_tests(tmp_path, ["deploy/watchdog/watchdog.xml"])
    assert plan["tests"] == ["tests/test_watchdog.py"]


def test_tracked_deploy_unnamed_falls_back_to_full_suite(tmp_path):
    _asset_repo(tmp_path, {
        "deploy/x.service": "[Service]\nExecStart=/bin/true",
    })
    plan = selector().select_tests(tmp_path, ["deploy/x.service"])
    assert plan["tests"] == ["tests"]
    assert plan["reason"].startswith("no mapped tests")


def test_docs_and_status_markdown_skipped_when_changed(tmp_path):
    _asset_repo(tmp_path, {
        "docs/readme.md": "# Docs",
        "status/card.md": "# Status",
    })
    plan = selector().select_tests(tmp_path, ["docs/readme.md", "status/card.md"])
    assert plan["tests"] == []


def test_tracked_deploy_allowlist_maps_to_tests_naming_it(tmp_path):
    _asset_repo(tmp_path, {
        "deploy/allowlist.txt": "service-a\nservice-b\n",
        "tests/test_deploy.py": "ALLOWLIST = 'deploy/allowlist.txt'",
    })
    plan = selector().select_tests(tmp_path, ["deploy/allowlist.txt"])
    assert plan["tests"] == ["tests/test_deploy.py"]


def test_tracked_deploy_markdown_maps_to_tests_naming_it(tmp_path):
    _asset_repo(tmp_path, {
        "deploy/notes.md": "# Deploy notes",
        "tests/test_deploy.py": "NOTES = 'deploy/notes.md'",
    })
    plan = selector().select_tests(tmp_path, ["deploy/notes.md"])
    assert plan["tests"] == ["tests/test_deploy.py"]


def test_untracked_deploy_asset_is_skipped(tmp_path):
    _asset_repo(tmp_path)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "x.txt").write_text("local scratch\n")
    assert selector().select_tests(tmp_path, ["deploy/x.txt"])["tests"] == []


def test_root_level_file_is_skipped(tmp_path):
    _asset_repo(tmp_path, {"README.txt": "hello\n"})
    assert selector().select_tests(tmp_path, ["README.txt"])["tests"] == []


def test_real_repo_deploy_watchdog_selects_w36_regressions():
    root = Path(__file__).resolve().parents[1]
    plan = selector().select_tests(root, ["deploy/watchdog/watchdog.xml"])
    assert "tests/test_w36_regressions.py" in plan["tests"]


def test_select_tests_within_filters_matching_tests(tmp_path):
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "src/pkg/api.py": "from .core import VALUE",
        "tests/test_api.py": "from pkg import api",
        "tests/test_other.py": "from pkg import core",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # Both test_api and test_other are affected by core.py, but within restricts to test_api
    plan = selector().select_tests(tmp_path, ["src/pkg/core.py"], within=["tests/test_api.py"])
    assert plan["tests"] == ["tests/test_api.py"]


def test_select_tests_within_directory(tmp_path):
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "tests/contract/test_inbox.py": "from pkg.core import VALUE",
        "tests/test_other.py": "from pkg.core import VALUE",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    plan = selector().select_tests(tmp_path, ["src/pkg/core.py"], within=["tests/contract"])
    assert plan["tests"] == ["tests/contract/test_inbox.py"]


def test_select_tests_within_empty_intersection(tmp_path):
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "tests/test_split_make_public.py": "from pkg.core import VALUE",
        "tests/test_keeper_pg_unit.py": "import os",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # core.py only affects test_split_make_public.py, within only has test_keeper_pg_unit.py
    plan = selector().select_tests(tmp_path, ["src/pkg/core.py"], within=["tests/test_keeper_pg_unit.py"])
    assert plan["tests"] == []


def test_select_tests_within_with_fallback(tmp_path):
    files = {
        "src/pkg/__init__.py": "",
        "tests/conftest.py": "",
        "tests/contract/test_inbox.py": "",
        "tests/test_keeper_pg_unit.py": "",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # conftest.py triggers fallback to full suite ["tests"], within restricts it to given paths
    plan = selector().select_tests(tmp_path, ["tests/conftest.py"],
                                  within=["tests/contract", "tests/test_keeper_pg_unit.py"])
    assert plan["tests"] == ["tests/contract", "tests/test_keeper_pg_unit.py"]


def test_affected_tests_cli_within_empty_intersection(tmp_path, capsys, monkeypatch):
    sel = selector()
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "tests/test_split_make_public.py": "from pkg.core import VALUE",
        "tests/test_keeper_pg_unit.py": "import os",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "affected_tests.py",
            "--base", "HEAD",
            "--changed", "src/pkg/core.py",
            "--within", "tests/test_keeper_pg_unit.py",
            "--run",
        ],
    )
    rc = sel.main(root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "no pg-affected tests" in captured.out


def test_cli_unknown_within_path_refuses(tmp_path, capsys, monkeypatch):
    """A stale or typo --within entry must never pass vacuously."""
    sel = selector()
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "tests/test_api.py": "from pkg import core",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "affected_tests.py",
            "--base", "HEAD",
            "--changed", "src/pkg/core.py",
            "--within", "tests/test_nope.py",
            "--run",
        ],
    )
    rc = sel.main(root=tmp_path)
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown --within path: tests/test_nope.py" in captured.err
    assert captured.out == ""


def test_cli_existing_within_file_and_dir_accepted(tmp_path, capsys, monkeypatch):
    sel = selector()
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "tests/contract/test_inbox.py": "from pkg.core import VALUE",
        "tests/test_api.py": "from pkg.core import VALUE",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "affected_tests.py",
            "--base", "HEAD",
            "--changed", "src/pkg/core.py",
            "--within", "tests/contract", "tests/test_api.py",
        ],
    )
    rc = sel.main(root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "unknown --within path" not in captured.err
    import json
    plan = json.loads(captured.out.splitlines()[0])
    assert sorted(plan["tests"]) == ["tests/contract/test_inbox.py", "tests/test_api.py"]


def test_run_invocation_passes_dist_loadgroup(tmp_path, monkeypatch):
    """The --run pytest argv must carry --dist loadgroup like the pg run."""
    import types

    sel = selector()
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "VALUE = 1",
        "tests/test_api.py": "from pkg import core",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    argv = {}

    def fake_run(cmd, cwd=None):
        argv["cmd"] = list(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sel, "subprocess", types.SimpleNamespace(run=fake_run))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "affected_tests.py",
            "--base", "HEAD",
            "--changed", "src/pkg/core.py",
            "--run",
        ],
    )
    rc = sel.main(root=tmp_path)
    assert rc == 0
    cmd = argv["cmd"]
    assert "--dist" in cmd
    assert cmd[cmd.index("--dist") + 1] == "loadgroup"


def test_ci_pr_uses_affected_and_nightly_keeps_full():
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1]
                              / ".github/workflows/tests.yml").read_text())
    # pytest job runs all affected tests
    pytest_steps = workflow["jobs"]["pytest"]["steps"]
    pytest_checkout = next(s for s in pytest_steps if s.get("uses") == "actions/checkout@v4")
    assert pytest_checkout["with"]["fetch-depth"] == 0
    pytest_cmd = next(s["run"] for s in pytest_steps if s.get("name", "").startswith("pytest"))
    assert 'pull_request' in pytest_cmd
    assert 'tools/affected_tests.py --base "$BASE_SHA" --run -n auto' in pytest_cmd
    assert 'python3 -m pytest -q -n auto' in pytest_cmd

    # pytest-postgres job restricts affected tests via --within
    pg_steps = workflow["jobs"]["pytest-postgres"]["steps"]
    pg_checkout = next(s for s in pg_steps if s.get("uses") == "actions/checkout@v4")
    assert pg_checkout["with"]["fetch-depth"] == 0
    pg_cmd = next(s["run"] for s in pg_steps if s.get("name", "").startswith("pytest"))
    assert 'pull_request' in pg_cmd
    assert '--within tests/contract tests/test_keeper_pg_unit.py tests/test_keeper_pg_parity.py tests/test_keeper_pg_copy.py tests/test_projector_ddl.py $FILES' in pg_cmd
    assert 'tools/affected_tests.py --base "$BASE_SHA"' in pg_cmd
    # on push to main, full PG set is unchanged
    assert 'tests/test_projector_ddl.py $FILES' in pg_cmd
    assert 'python3 -m pytest -q -n auto --dist loadgroup --durations=30 tests/contract tests/test_keeper_pg_unit.py tests/test_keeper_pg_parity.py tests/test_keeper_pg_copy.py' in pg_cmd

    # PyYAML's YAML 1.1 loader treats the GitHub key `on` as boolean True.
    assert workflow.get("on", workflow.get(True))["schedule"]
