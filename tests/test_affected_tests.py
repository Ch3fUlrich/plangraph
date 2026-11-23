import importlib.util
import os
from pathlib import Path

import pytest


def selector():
    spec = importlib.util.spec_from_file_location(
        "affected_tests", Path(__file__).resolve().parents[1] / "tools/affected_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('build_code', [0, 1])
def test_local_fleet_selection_builds_pg_spawnd_before_pytest(tmp_path, monkeypatch, build_code):
    import sys
    from types import SimpleNamespace

    module = selector()
    fleet = 'tests/test_fleet_snapshot.py'
    (tmp_path / 'tests').mkdir()
    (tmp_path / fleet).write_text('def test_build(): pass\n')
    monkeypatch.setattr(sys, 'argv', ['affected_tests', '--base', 'HEAD', '--changed', fleet,
                                    '--run', '--local', '--files-only', '--workers', '0'])
    monkeypatch.setattr(module, 'test_environment', lambda root, env: dict(env))
    monkeypatch.setattr(module, 'tree_hash', lambda root: 'fixture')
    calls = []

    def run(command, *, cwd, env, **kwargs):
        assert cwd == tmp_path
        if command[:3] == [sys.executable, '-m', 'pytest']:
            assert env['PLANGRAPH_TEST_FLEET_SPAWND'] == calls[-1][2]
        calls.append(command)
        return SimpleNamespace(returncode=build_code if command[0] == 'cargo' else 0)

    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.main(root=tmp_path) == build_code
    assert calls[0] == ['cargo', 'build', '--locked', '--manifest-path', 'rust/Cargo.toml',
                        '-p', 'pg-spawnd']
    assert len(calls) == (1 if build_code else 3)
    if not build_code:
        assert calls[1][:2] == [sys.executable, 'tests/_spawnd.py']
        assert calls[2][:3] == [sys.executable, '-m', 'pytest']


@pytest.mark.parametrize("changed,expected", [
    (["src/pkg/core.py"], ["tests/test_api.py"]),
    (["tests/test_api.py"], ["tests/test_api.py"]),
    (["tests/conftest.py"], ["tests"]),
    (["pyproject.toml"], ["tests"]),
    ([".github/workflows/check.yml"], ["tests/test_affected_tests.py"]),
    (["src/pkg/__init__.py"], ["tests/test_api.py"]),
    (["unknown.txt"], []),
    (["docs/guide.md", "status/card", "build/generated.py", "scratch.py"], []),
    (["tests/notes.md", "src/pkg/README.md"], []),
    (["src/pkg/missing.py"], ["tests"]),  # unresolved deletion: conservative fallback
    (["tools/new_tool.py"], ["tests"]),  # unresolved deletion: conservative fallback
    (["out/generated.py", "docs/example.py", "status/card.md"], []),
    (["docs/guide.md", "src/pkg/core.py", "status/card"], ["tests/test_api.py"]),
    (["conftest.py"], ["tests"]),
    (["tests/helper.py"], ["tests"]),
    (["src/pkg/orphan.py"], []),  # unmapped code: bounded smoke, no full suite
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
    if changed == [".github/workflows/check.yml"]:
        files["tests/test_affected_tests.py"] = ""
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert selector().select_tests(tmp_path, changed)["tests"] == expected


@pytest.mark.parametrize('changed', ['rust/crate/tests/fixture.rs', 'tests/test_fixture.py'])
def test_ci_selects_subprocess_guard_for_both_test_languages(tmp_path, changed):
    guard = 'tests/test_spawner_import_guard.py'
    for relative in (guard, changed):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert guard in selector().select_tests(tmp_path, [changed])['tests']


def test_browser_tests_never_selected(tmp_path):
    # D-2821: tests/browser runs in its own CI job; an explicit path would collect it in pytest.
    for name, text in {"src/pkg/__init__.py": "", "src/pkg/core.py": "VALUE = 1",
                       "tests/test_api.py": "from pkg import core", "tests/browser/test_ui.py": "from pkg import core"}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    assert selector().select_tests(tmp_path, ["src/pkg/core.py"])["tests"] == ["tests/test_api.py"]


@pytest.mark.parametrize('statement', ['from pkg.cli import main', 'import pkg.cli'])
def test_function_local_import_selects_caller(tmp_path, statement):
    files = {'src/pkg/__init__.py': '', 'src/pkg/cli.py': 'def main(): pass',
             'tests/test_caller.py': f'def test_cli():\n    {statement}\n',
             'tests/test_other.py': 'def test_other(): pass'}
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert selector().select_tests(tmp_path, ['src/pkg/cli.py'])['tests'] == ['tests/test_caller.py']


def test_spawner_cli_change_selects_w9_local_caller():
    root = Path(__file__).resolve().parents[1]
    module = selector()
    changed = ['src/plangraph/spawner/cli.py']
    caller = 'tests/test_w9_regressions.py'
    plan = module.select_tests(root, changed, local=True, files_only=True, within=[caller])
    assert plan['tests'] == [caller]
    assert caller in module.select_tests(root, changed)['tests']


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
    assert plan["tests"] == []  # smoke contract: no test names src/pkg, none is the selector's own


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
    assert plan["tests"] == []  # top-level dir is too broad to name tests; smoke is empty here


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
    _asset_repo(tmp_path, {"README.txt": "hello\n", "rust-toolchain.toml": "[toolchain]",
                           "tests/test_pin.py": "PIN = 'rust-toolchain.toml'"})
    assert selector().select_tests(tmp_path, ["README.txt"])["tests"] == []
    assert selector().select_tests(tmp_path, ["rust-toolchain.toml"])["tests"] == ["tests/test_pin.py"]


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

    def fake_run(cmd, cwd=None, env=None, *, pass_fds=()):
        argv["env"] = env
        argv["cmd"] = list(cmd)
        argv["pass_fds"] = pass_fds
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setenv('GIT_DIR', 'caller-binding')
    installed = tmp_path / 'installed'
    monkeypatch.setenv('PYTHONPATH', str(installed))
    monkeypatch.setattr(sel.subprocess, 'check_output', lambda *a, **k: 'GIT_DIR\n' if '--local-env-vars' in a[0] else b'')
    monkeypatch.setattr(sel.subprocess, 'run', fake_run)
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
    assert "GIT_DIR" not in argv["env"]
    assert argv["env"]["PYTHONPATH"].split(os.pathsep)[-2:] == [
        str(tmp_path / 'src'), str(installed)]
    cmd = argv["cmd"]
    assert bool(argv["pass_fds"]) == (os.name == "posix")
    assert "--dist" in cmd
    assert cmd[cmd.index("--dist") + 1] == "loadgroup"


@pytest.mark.parametrize('launch', [
    'subprocess.run([sys.executable, "-m", "pkg.cli"])',
    'subprocess.run("python3 -m pkg.cli run", shell=True)',
    'subprocess.run(["bash", "tools/outer.sh"])',
])
def test_subprocess_consumers_follow_import_graph(tmp_path, launch):
    _asset_repo(tmp_path, {
        'src/pkg/cli.py': 'from .core import VALUE',
        'tools/outer.sh': 'bash tools/inner.sh\n',
        'tools/inner.sh': 'python3 -m pkg.cli run\n',
        'tests/test_process.py': launch,
    })
    plan = selector().select_tests(tmp_path, ['src/pkg/core.py'])
    assert 'tests/test_process.py' in plan['tests']
    assert 'tests/test_other.py' not in plan['tests']


@pytest.mark.parametrize('statement', [
    'from pkg.api import VALUE', 'from pkg import api', 'from pkg import api as renamed',
    'from pkg import (\n    api,\n)',
])
def test_shell_inline_python_consumer(tmp_path, statement):
    _asset_repo(tmp_path, {
        'tools/smoke.sh': f"python3 - <<'PY'\n{statement}\nPY\n",
        'tests/test_process.py': 'SCRIPT = "tools/smoke.sh"',
    })
    assert 'tests/test_process.py' in selector().select_tests(
        tmp_path, ['src/pkg/core.py'])['tests']


def test_spawner_admission_selects_899_subprocess_regressions():
    root = Path(__file__).resolve().parents[1]
    plan = selector().select_tests(root, ['src/plangraph/spawner/base_fresh.py'])
    assert {'tests/test_spawner_overflow.py', 'tests/test_spawner_overflow_caps.py',
            'tests/test_run_exit_live.py'} <= set(plan['tests'])


@pytest.mark.parametrize('path,importer,local', [
    ('src/pkg/core.py', 'src/pkg/api.py', False),
    ('tests/helper.py', 'tests/test_api.py', True),
    ('tests/helper.py', 'tests/test_api.py', False),
    ('tools/helper.py', 'tests/test_api.py', False),
])
def test_deleted_module_selects_base_importers(tmp_path, path, importer, local):

    module = path.removeprefix('src/').removesuffix('.py').replace('/', '.')
    git = _asset_repo(tmp_path, {
        path: 'VALUE = 1', importer: f'from {module} import VALUE',
    })
    base = git('rev-parse', 'HEAD').strip()
    (tmp_path / path).unlink()
    # Use the base edges even when the head no longer has the old import.
    (tmp_path / importer).write_text('VALUE = 2')
    git('add', '.')
    git('commit', '-qm', 'delete and change importer')
    plan = selector().select_tests(tmp_path, [path], base=base, local=local, files_only=True)
    assert plan['tests'] == ['tests/test_api.py']


def test_deleted_unresolved_module_falls_back_to_suite(tmp_path):
    _asset_repo(tmp_path, {'src/pkg/deleted.py': 'VALUE = 1'})
    (tmp_path / 'src/pkg/deleted.py').unlink()
    assert selector().select_tests(tmp_path, ['src/pkg/deleted.py'])['tests'] == ['tests']


@pytest.mark.parametrize('launch', [
    'subprocess.run([sys.executable, "-m", "pkg"])',
    'subprocess.run("python3 -m pkg", shell=True)',
    'SCRIPT = "tools/launch.sh"',
])
def test_package_launch_follows_main(tmp_path, launch):
    _asset_repo(tmp_path, {
        'src/pkg/__main__.py': 'from .core import VALUE',
        'tools/launch.sh': 'python3 -m pkg',
        'tests/test_process.py': launch,
    })
    for changed in ['src/pkg/core.py', 'src/pkg/__main__.py']:
        assert 'tests/test_process.py' in selector().select_tests(tmp_path, [changed])['tests']


def test_shell_calls_python_tool_transitively(tmp_path):
    _asset_repo(tmp_path, {
        'tools/entry.py': 'from pkg.core import VALUE',
        'tools/launch.sh': 'python3 tools/entry.py',
        'tests/test_process.py': 'SCRIPT = "tools/launch.sh"',
    })
    assert 'tests/test_process.py' in selector().select_tests(
        tmp_path, ['src/pkg/core.py'])['tests']


def test_deleted_module_keeps_literal_consumers(tmp_path):
    _asset_repo(tmp_path, {
        'tools/entry.py': '',
        'tests/test_importer.py': 'from tools import entry',
        'tests/test_process.py': 'subprocess.run(["python3", "tools/entry.py"])',
        'src/pkg/direct.py': '',
        'tests/test_direct.py': 'subprocess.run(["python3", "src/pkg/direct.py"])',
    })
    (tmp_path / 'tools/entry.py').unlink()
    (tmp_path / 'src/pkg/direct.py').unlink()
    plan = selector().select_tests(tmp_path, ['tools/entry.py', 'src/pkg/direct.py'])
    assert plan['tests'] == ['tests/test_direct.py', 'tests/test_importer.py', 'tests/test_process.py']


@pytest.mark.parametrize('path', ['tests/test_removed.py', 'tests/conftest.py'])
def test_unresolved_deleted_tests_require_suite(tmp_path, path):
    _asset_repo(tmp_path, {path: ''})
    (tmp_path / path).unlink()
    # The brief requires importer coverage or the whole suite for *every*
    # deleted Python file under tests, including leaf tests and conftest.
    for files_only in [False, True]:
        assert selector().select_tests(tmp_path, [path], files_only=files_only)['tests'] == ['tests']


def test_deleted_conftest_keeps_implicit_fixture_consumers(tmp_path):
    _asset_repo(tmp_path, {
        'tests/conftest.py': 'VALUE = 1',
        'tests/test_importer.py': 'from conftest import VALUE',
        'tests/test_implicit.py': 'def test_implicit(fixture): pass',
    })
    (tmp_path / 'tests/conftest.py').unlink()
    assert selector().select_tests(tmp_path, ['tests/conftest.py'])['tests'] == ['tests']


def test_python_tool_in_production_prose_is_not_a_call(tmp_path):
    _asset_repo(tmp_path, {
        'tools/check.py': '',
        'src/pkg/api.py': 'PROMPT = "Run python3 tools/check.py"',
    })
    assert selector().select_tests(tmp_path, ['tools/check.py'])['tests'] == []


@pytest.mark.parametrize('deleted', [False, True])
def test_tool_bare_imports_follow_all_candidates(tmp_path, deleted):
    _asset_repo(tmp_path, {
        'tools/hooks/rules.py': 'VALUE = 1',
        'tools/other/rules.py': 'VALUE = 2',
        'tools/hooks/entry.py': 'import rules',
        'tests/test_bare.py': 'import entry',
        'tests/test_rules.py': 'from rules import VALUE',
    })
    path = 'tools/hooks/rules.py'
    if deleted:
        (tmp_path / path).unlink()
    assert {'tests/test_bare.py', 'tests/test_rules.py'} <= set(
        selector().select_tests(tmp_path, [path])['tests'])


@pytest.mark.parametrize('call', ['run', 'Popen', 'check_output'])
def test_python_tool_keyword_argv(tmp_path, call):
    _asset_repo(tmp_path, {
        'tools/entry.py': 'from pkg.core import VALUE',
        'src/pkg/api.py': f'subprocess.{call}(args=["python3", "tools/entry.py"])',
    })
    assert 'tests/test_api.py' in selector().select_tests(
        tmp_path, ['src/pkg/core.py'])['tests']


def test_real_hook_rules_select_implicit_importers():
    root = Path(__file__).resolve().parents[1]
    for path in ['tools/hooks/bash_guard_rules.py', 'tools/hooks/hook_common.py']:
        plan = selector().select_tests(root, [path])
        assert {'tests/hooks/test_bash_guard_allow.py', 'tests/hooks/test_bash_guard_deny.py'} <= set(plan['tests'])


@pytest.mark.parametrize('command', [
    "python3 -c 'from pkg.core import VALUE; print(VALUE)'",
    'PYTHONPATH=src python3 -c "import pkg.core"',
    'python3 -c "from pkg import (\n    api,\n)"',
    "bash -c 'python3 -c \"import pkg.core\"'",
    "python3 - <<-'PY'\n\tfrom pkg import (\n\t    api,\n\t)\nPY\n",
])
def test_shell_literal_python_commands(tmp_path, command):
    _asset_repo(tmp_path, {
        'tools/launch.sh': command,
        'tests/test_process.py': 'SCRIPT = "tools/launch.sh"',
    })
    assert 'tests/test_process.py' in selector().select_tests(
        tmp_path, ['src/pkg/core.py'])['tests']


@pytest.mark.parametrize('command', [
    "out=$(python3 -c 'import pkg.api; print(1)')",
    "(python3 -c 'import pkg.api')",
    "x=`python3 -c 'import pkg.api'`",
    "python3 -c 'import pkg.api'&&echo",
    "python3 -c 'import pkg.api'|cat",
    'echo "$(python3 -c \'import pkg.api\')"',
    'python3 -c "import pkg.api as m; m.run($ARG)"',
    'python3 -c "from pkg import api as m; m.run($ARG)"',
])
def test_shell_imports_survive_shell_syntax(tmp_path, command):
    _asset_repo(tmp_path, {
        'tools/launch.sh': command,
        'tests/test_process.py': 'SCRIPT = "tools/launch.sh"',
    })
    assert 'tests/test_process.py' in selector().select_tests(
        tmp_path, ['src/pkg/core.py'])['tests']

def test_local_init_selects_explicit_imports_and_changed_exports(tmp_path):
    initializer = "src/pkg/__init__.py"
    _asset_repo(tmp_path, {
        initializer: "from .core import VALUE as public\nfrom .api import VALUE as stable\n",
        "src/pkg/other.py": "VALUE = 2",
        "tests/test_package.py": "import pkg",
        "tests/test_dynamic_package.py": 'importlib.import_module("pkg")',
        "tests/test_project_name.py": 'PROJECT = "pkg"',
        "tests/test_public.py": "from pkg import public",
        "tests/test_stable.py": "from pkg import stable",
        "tests/test_deep.py": "import pkg.api",
        "tests/test_inherited.py": "from pkg.api import VALUE",
    })
    (tmp_path / initializer).write_text(
        "from .other import VALUE as public\nfrom .api import VALUE as stable\n"
    )
    module = selector()
    local = module.select_tests(tmp_path, [initializer], local=True, files_only=True)
    assert local["tests"] == ["tests/test_dynamic_package.py", "tests/test_package.py",
                              "tests/test_public.py"]
    ci = module.select_tests(tmp_path, [initializer])
    assert "tests/test_deep.py" in ci["tests"]
    api = module.select_tests(tmp_path, ["src/pkg/api.py"], local=True, files_only=True)
    assert api["tests"] == ["tests/test_api.py", "tests/test_deep.py", "tests/test_inherited.py"]


@pytest.mark.parametrize("local", [False, True])
def test_workflow_only_changes_select_ci_regressions(tmp_path, local):
    regressions = {"test_ci_select.py", "test_ci_select_rust_inputs.py",
                   "test_workflow_archive.py", "test_affected_tests.py",
                   "test_affected_tests_head.py", "test_split_ci_test_affected_tests.py"}
    directory = tmp_path / "tests"
    directory.mkdir()
    for name in regressions | {"test_other.py"}:
        (directory / name).touch()
    plan = selector().select_tests(tmp_path, [".github/workflows/tests.yml"],
                                   local=local, files_only=local)
    assert plan["tests"] == sorted(f"tests/{name}" for name in regressions)
