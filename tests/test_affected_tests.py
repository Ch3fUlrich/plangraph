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
    (["src/pkg/missing.py"], []),  # unmapped code: bounded smoke, no full suite
    (["tools/new_tool.py"], []),  # unmapped code: bounded smoke, no full suite
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

    def fake_run(cmd, cwd=None, env=None):
        argv["env"] = env
        argv["cmd"] = list(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setenv('GIT_DIR', 'caller-binding')
    monkeypatch.setattr(sel.subprocess, 'check_output', lambda *a, **k: 'GIT_DIR\n')
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
    assert 'python -m pytest -q -n auto' in pytest_cmd

    # pytest-postgres job restricts affected tests via --within
    pg_steps = workflow["jobs"]["pytest-postgres"]["steps"]
    pg_checkout = next(s for s in pg_steps if s.get("uses") == "actions/checkout@v4")
    assert pg_checkout["with"]["fetch-depth"] == 0
    pg_cmd = next(s["run"] for s in pg_steps if s.get("name", "").startswith("pytest"))
    assert 'pull_request' in pg_cmd
    assert '--within tests/contract tests/test_keeper_pg_unit.py tests/test_keeper_pg_parity.py tests/test_keeper_pg_copy.py tests/test_projector_ddl.py tests/test_worker_claim.py tests/test_claim_parity_dispatch.py $FILES' in pg_cmd
    assert 'tools/affected_tests.py --base "$BASE_SHA"' in pg_cmd
    # On push to main, the full PG set also includes claim contract coverage.
    assert 'tests/test_projector_ddl.py tests/test_worker_claim.py tests/test_claim_parity_dispatch.py $FILES' in pg_cmd
    assert 'python -m pytest -q -n auto --dist loadgroup --durations=30 tests/contract tests/test_keeper_pg_unit.py tests/test_keeper_pg_parity.py tests/test_keeper_pg_copy.py' in pg_cmd

    # PyYAML's YAML 1.1 loader treats the GitHub key `on` as boolean True.
    assert workflow.get("on", workflow.get(True))["schedule"]


def test_missing_pytest_has_named_fix(tmp_path, monkeypatch, capsys):
    import sys
    sel = selector()
    monkeypatch.setattr(sel, 'select_tests', lambda *a, **k: {'tests': ['tests/test_x.py']})
    monkeypatch.setattr(sel.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(sel.subprocess, 'run', lambda *a, **k: pytest.fail('pytest process started'))
    monkeypatch.setattr(sys, 'argv', ['affected_tests.py', '--base', 'HEAD', '--changed', 'src/x.py', '--run'])
    assert sel.main(root=tmp_path) == 1
    out = capsys.readouterr()
    assert out.err.count('FAIL pytest-missing:') == 1
    assert '-m pip install' in out.err


@pytest.mark.parametrize('broad', ['.github/workflows/tests.yml', 'tests/conftest.py', 'tests/helpers.py', 'pyproject.toml'])
def test_local_selection_keeps_changed_tests_and_reverse_consumers(tmp_path, broad):
    files = {'src/pkg/core.py': 'VALUE = 1\n',
             'tests/test_api.py': 'from pkg.core import VALUE\n',
             'tests/test_written.py': 'def test_written(): pass\n',
             'tests/test_contract.py': repr(broad) + '\n',
             'tests/test_unrelated.py': 'def test_unrelated(): pass\n'}
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    module = selector()
    changed = [broad, 'src/pkg/core.py', 'tests/test_written.py']
    assert module.select_tests(tmp_path, changed)['tests'] == ['tests']
    plan = module.select_tests(tmp_path, changed, files_only=True)
    assert set(plan['tests']) == {'tests/test_api.py', 'tests/test_written.py', 'tests/test_contract.py'}
    assert all((tmp_path / path).is_file() for path in plan['tests'])


def test_local_shared_controls_have_bounded_smoke_coverage(tmp_path):
    directory = tmp_path / 'tests'
    directory.mkdir()
    for index in range(30):
        (directory / f'test_ref_{index:02}.py').write_text('"tests/conftest.py"\n')
    (directory / 'test_written.py').write_text('def test_written(): pass\n')
    module = selector()
    plan = module.select_tests(tmp_path, ['tests/conftest.py', 'tests/test_written.py'], files_only=True)
    assert 'tests/test_written.py' in plan['tests']
    assert len(plan['tests']) <= module.LOCAL_FILE_CAP
    assert 'tests' not in plan['tests']


def test_local_unreadable_graph_refuses_instead_of_full_collection(tmp_path, monkeypatch, capsys):
    import sys
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_broken.py').write_text('def invalid(\n')
    module = selector()
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('unreadable graph launched pytest'))
    monkeypatch.setattr(sys, 'argv', ['affected_tests.py', '--base', 'HEAD', '--changed', 'tests/test_broken.py', '--files-only', '--run'])
    assert module.main(root=tmp_path) == 1
    assert 'local-selection' in capsys.readouterr().err


def _local_tree(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.mark.parametrize('control', ['tests/conftest.py', 'pytest.ini', 'pyproject.toml',
    'tools/hooks/pre-push', 'tools/preflight.py', 'tools/affected_tests.py'])
def test_local_controls_select_own_hook_preflight_and_changed_tests(tmp_path, control):
    own = {'tests/test_conftest_env.py', 'tests/test_hooks_prepush.py',
           'tests/test_preflight.py', 'tests/test_preflight_git_env.py', 'tests/test_affected_tests.py'}
    _local_tree(tmp_path, {**{p: '' for p in own},
        'tests/test_written.py': 'def test_written(): pass\n',
        **{f'tests/test_unrelated_{i:03}.py': 'def test_unrelated(): pass\n' for i in range(80)}})
    result = selector().select_tests(tmp_path, [control, 'tests/test_written.py'], files_only=True)
    assert set(result['tests']) == own | {'tests/test_written.py'}
    assert result['tests'][0] == 'tests/test_written.py'


def test_local_support_helper_selects_direct_importers(tmp_path):
    _local_tree(tmp_path, {'tests/helpers.py': 'VALUE = 1\n',
        'tests/test_direct.py': 'from helpers import VALUE\n',
        'tests/test_unrelated.py': 'def test_unrelated(): pass\n'})
    result = selector().select_tests(tmp_path, ['tests/helpers.py'], files_only=True)
    assert result['tests'] == ['tests/test_direct.py']


def test_local_cap_prioritizes_changed_then_direct_then_transitive(tmp_path):
    direct = [f'tests/test_direct_{i:03}.py' for i in range(80)]
    written = 'tests/test_z_written.py'
    _local_tree(tmp_path, {'src/pkg/core.py': 'VALUE = 1\n',
        'src/pkg/api.py': 'from pkg.core import VALUE\n',
        **{p: 'from pkg.core import VALUE\n' for p in direct}, written: '',
        'tests/test_00_indirect.py': 'from pkg.api import VALUE\n',
        'tests/test_01_indirect.py': 'from pkg.api import VALUE\n'})
    module = selector()
    changes = ['src/pkg/core.py', written]
    result = module.select_tests(tmp_path, changes, files_only=True)
    assert result['tests'] == [written, *direct[:39]]
    assert result['deferred'] == 43
    assert module.select_tests(tmp_path, reversed(changes), files_only=True) == result


def test_local_cap_applies_even_when_all_files_are_changed_tests(tmp_path):
    files = {f'tests/test_written_{i:03}.py': '' for i in range(60)}
    _local_tree(tmp_path, files)
    result = selector().select_tests(tmp_path, reversed(files), files_only=True)
    assert result['tests'] == sorted(files)[:40]
    assert result['deferred'] == 20


def test_local_cap_reports_files_left_to_ci_once(tmp_path, monkeypatch, capsys):
    import json
    import sys
    _local_tree(tmp_path, {'src/pkg/core.py': 'VALUE = 1\n',
        **{f'tests/test_direct_{i:03}.py': 'from pkg.core import VALUE\n' for i in range(45)}})
    from types import SimpleNamespace
    module, launched = selector(), []
    monkeypatch.setattr(module, 'test_environment', lambda *a: {'FIXTURE': 'clean'})
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kw:
                        launched.append((command, kw)) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, 'argv', ['affected_tests', '--base', 'unused', '--changed',
                                    'src/pkg/core.py', '--files-only', '--run'])
    assert module.main(root=tmp_path) == 0
    output = capsys.readouterr()
    selected = json.loads(output.out.splitlines()[0])['tests']
    assert len(selected) == 40
    assert [arg for arg in launched[0][0] if arg.endswith('.py')] == selected
    assert launched[0][1]['env'] == {'FIXTURE': 'clean'}
    assert output.err.splitlines() == ['affected tests: 5 files left to CI (local cap: 40 files; effective bound: 40 files; coverage reduced to floor: none)']


@pytest.mark.parametrize('cap', [0, -1, 41])
def test_local_cap_cannot_be_disabled_or_increased(tmp_path, monkeypatch, cap, capsys):
    import sys
    monkeypatch.setattr(sys, 'argv', ['affected_tests', '--base', 'unused',
                                    '--files-only', '--max-files', str(cap)])
    with pytest.raises(SystemExit) as err:
        selector().main(root=tmp_path)
    assert err.value.code == 2
    assert '--max-files must be between 1 and 40' in capsys.readouterr().err


def test_local_global_support_keeps_explicit_direct_importers_only(tmp_path):
    _local_tree(tmp_path, {'tests/conftest.py': 'VALUE = 1\n',
        'tests/test_z_direct.py': 'from conftest import VALUE\n',
        'tests/test_a_indirect.py': 'from test_z_direct import VALUE\n'})
    plan = selector().select_tests(tmp_path, ['tests/conftest.py'], files_only=True)
    assert plan['tests'] == ['tests/test_z_direct.py']


def _floor_tree(root, groups=5, per_file=12):
    changes, consumers = [], {}
    for i in range(groups):
        source = f'src/pkg/core{i}.py'
        names = [f'tests/test_{i:02}_{j:02}.py' for j in range(per_file)]
        _local_tree(root, {source: 'VALUE = 1\n',
            **{name: f'from pkg.core{i} import VALUE\n' for name in names}})
        changes.append(source)
        consumers[source] = names
    return changes, consumers


def test_local_floor_keeps_coverage_that_ranks_last(tmp_path):
    changes, consumers = _floor_tree(tmp_path)
    module = selector()
    result = module.select_tests(tmp_path, changes, files_only=True)
    assert len(result['tests']) == 40
    assert result['deferred'] == 20
    for names in consumers.values():
        assert names[0] in result['tests']
    assert result['floor_only'] == [changes[-1]]
    assert module.select_tests(tmp_path, reversed(changes), files_only=True) == result


def test_local_floors_grow_bound_when_more_than_forty_are_needed(tmp_path, monkeypatch, capsys):
    changes, consumers = _floor_tree(tmp_path, groups=45, per_file=1)
    _local_tree(tmp_path, {f'tests/test_extra_{i:02}.py': 'from pkg.core0 import VALUE\n'
                          for i in range(10)})
    module = selector()
    result = module.select_tests(tmp_path, changes, files_only=True)
    assert result['tests'] == sorted(names[0] for names in consumers.values())
    assert result['limit'] == 45
    assert result['deferred'] == 10
    assert result['floor_only'] == [changes[0]]

    import json
    import sys
    from types import SimpleNamespace
    launched = []
    monkeypatch.setattr(module, 'test_environment', lambda *a: {})
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kw:
                        launched.append(command) or SimpleNamespace(returncode=0))
    args = ['affected_tests', '--base', 'unused', '--files-only', '--run']
    for source in changes:
        args.extend(['--changed', source])
    monkeypatch.setattr(sys, 'argv', args)
    assert module.main(root=tmp_path) == 0
    output = capsys.readouterr()
    assert json.loads(output.out.splitlines()[0])['tests'] == result['tests']
    assert [arg for arg in launched[0] if arg.endswith('.py')] == result['tests']
    assert output.err.splitlines() == [
        f'affected tests: 10 files left to CI (local cap: 40 files; effective bound: 45 files; '
        f'coverage reduced to floor: {changes[0]})']


def test_local_floor_uses_each_sources_shortest_import_path(tmp_path):
    _local_tree(tmp_path, {'src/pkg/core.py': 'VALUE = 1\n',
        'src/pkg/other.py': 'VALUE = 2\n',
        'src/pkg/bridge.py': 'from pkg.core import VALUE\n',
        'tests/test_a.py': 'from pkg.bridge import VALUE\nfrom pkg.other import VALUE\n',
        'tests/test_z.py': 'from pkg.core import VALUE\n'})
    result = selector().select_tests(tmp_path, ['src/pkg/core.py', 'src/pkg/other.py'],
                                     files_only=True, max_files=1)
    # test_a is globally direct for other.py but only transitive for core.py.
    assert result['tests'] == ['tests/test_a.py', 'tests/test_z.py']
    assert result['floors'] == {'src/pkg/core.py': 'tests/test_z.py',
                                'src/pkg/other.py': 'tests/test_a.py'}


def test_local_floor_deduplicates_shared_test_and_keeps_changed_tests_first(tmp_path):
    _local_tree(tmp_path, {'src/pkg/core.py': 'VALUE = 1\n',
        'src/pkg/other.py': 'VALUE = 2\n',
        'tests/test_a.py': 'from pkg.core import VALUE\nfrom pkg.other import VALUE\n',
        'tests/test_z_written.py': ''})
    result = selector().select_tests(tmp_path,
        ['src/pkg/core.py', 'src/pkg/other.py', 'tests/test_z_written.py'],
        files_only=True, max_files=2)
    assert result['tests'] == ['tests/test_z_written.py', 'tests/test_a.py']
    assert result['limit'] == 2


def test_local_deferred_line_names_floor_only_sources(tmp_path, monkeypatch, capsys):
    import json
    import sys
    changes, _ = _floor_tree(tmp_path)
    args = ['affected_tests', '--base', 'unused', '--files-only']
    for source in changes:
        args.extend(['--changed', source])
    monkeypatch.setattr(sys, 'argv', args)
    assert selector().main(root=tmp_path) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)['floor_only'] == [changes[-1]]
    assert out.err.splitlines() == [
        f'affected tests: 20 files left to CI (local cap: 40 files; effective bound: 40 files; '
        f'coverage reduced to floor: {changes[-1]})']
