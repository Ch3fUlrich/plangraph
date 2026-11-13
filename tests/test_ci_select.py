from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import subprocess

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ci_select.py"
spec = importlib.util.spec_from_file_location("ci_select", MODULE_PATH)
assert spec is not None and spec.loader is not None
ci_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_select)

SUITES = ci_select.SUITES
select = ci_select.select
gate = ci_select.gate
main = ci_select.main


def test_docs_only_change():
    res = select(["docs/a.md", "README.md", "CHANGELOG.md"])
    assert res["full"] is False
    assert res["pytest"] is False
    assert res["pytest-postgres"] is False
    assert res["rust"] is False
    assert res["rust-worker"] is False
    assert res["rust-spike"] is False


def test_rust_worker_change():
    res = select(["rust/worker/src/x.rs"])
    assert res["pytest"] is True
    assert res["pytest-postgres"] is True
    assert res["rust-worker"] is True
    assert res["rust-spike"] is False
    assert res["rust"] is False
    assert res["full"] is False


def test_rust_spikes_change():
    res = select(["rust/spikes/a.rs"])
    assert res["rust-spike"] is True
    assert res["rust"] is False
    assert res["rust-worker"] is False
    assert res["pytest"] is True
    assert res["pytest-postgres"] is True
    assert res["full"] is False


def test_rust_core_change():
    res = select(["rust/core/a.rs"])
    assert res["rust"] is True
    assert res["rust-worker"] is False
    assert res["rust-spike"] is False
    assert res["pytest"] is True
    assert res["pytest-postgres"] is True
    assert res["full"] is False


def test_rust_worker_core_change():
    res = select(["rust/worker-core/a.rs"])
    assert res["rust"] is True
    assert res["rust-worker"] is True
    assert res["rust-spike"] is False
    assert res["pytest"] is True
    assert res["pytest-postgres"] is True
    assert res["full"] is False


def test_src_change():
    res = select(["src/module.py"])
    assert res["pytest"] is True
    assert res["pytest-postgres"] is True
    assert res["rust"] is False
    assert res["rust-worker"] is False
    assert res["rust-spike"] is False
    assert res["full"] is False


@pytest.mark.parametrize("paths", [
    [], ["docs/x.md"], ["skills/a/SKILL.md"], [".github/workflows/tests.yml"],
    ["src/m.py"], ["rust/worker/src/a.rs"], ["LICENSE"]])
def test_always_jobs_are_never_a_selection(paths):
    res = select(paths)
    assert not set(ci_select.ALWAYS) & set(res)


@pytest.mark.parametrize("job", ci_select.ALWAYS)
@pytest.mark.parametrize("result", [None, "skipped", "failure"])
def test_gate_requires_always_jobs_whatever_select_says(job, result):
    """scrub and file-size run on every change: the gate refuses one that is
    missing, skipped or red even when select's outputs name nothing at all."""
    needs = {"select": {"result": "success", "outputs": {s: "false" for s in SUITES}}}
    needs.update({j: {"result": "success"} for j in ci_select.ALWAYS})
    if result is None:
        del needs[job]
    else:
        needs[job] = {"result": result}
    assert gate(needs) == ["%s: %s" % (job, result or "missing")]


def test_workflow_gate_needs_every_always_job():
    import yaml
    wf = yaml.safe_load((MODULE_PATH.parents[1] / ".github" / "workflows" / "tests.yml").read_text())
    needs = wf["jobs"]["ci-gate"]["needs"]
    for job in ci_select.ALWAYS:
        assert job in needs
        assert "if" not in wf["jobs"][job] and "needs" not in wf["jobs"][job]
    # the ratchet base per event: push -> the pushed-over sha (never the zero
    # sha), PR -> origin/main, schedule/dispatch -> HEAD~1
    base = [s for s in wf["jobs"]["file-size"]["steps"] if "env" in s][0]["env"]
    assert base["FILESIZE_REQUIRE_BASE"] == (
        "${{ github.event_name == 'push' && github.event.before != '" + "0" * 40
        + "' && github.event.before || github.event_name == 'pull_request' "
        "&& 'origin/main' || 'HEAD~1' }}")
    assert wf["jobs"]["file-size"]["steps"][0]["with"]["fetch-depth"] == 0
    cl_base = [s for s in wf["jobs"]["changelog-docs"]["steps"] if "env" in s][0]["env"]
    assert cl_base["CHANGELOG_BASE"] == base["FILESIZE_REQUIRE_BASE"]
    assert wf["jobs"]["changelog-docs"]["steps"][0]["with"]["fetch-depth"] == 0


def test_skill_page_change_runs_pytest():
    res = select(["skills/routing-registry/references/remove.md"])
    assert res["pytest"] is True
    assert res["pytest-postgres"] is False
    assert res["rust"] is False
    assert res["full"] is False


def test_skill_non_page_change_is_unknown_full():
    res = select(["skills/routing-registry/scripts/x.sh"])
    assert res["full"] is True


def test_ci_github_workflow_full():
    res = select([".github/workflows/tests.yml"])
    assert res["full"] is True
    for s in SUITES:
        assert res[s] is True


def test_requirements_ci_full():
    res = select(["requirements-ci.txt"])
    assert res["full"] is True
    for s in SUITES:
        assert res[s] is True


def test_tools_ci_select_full():
    res = select(["tools/ci_select.py"])
    assert res["full"] is True
    for s in SUITES:
        assert res[s] is True


def test_unknown_path_mixed_with_docs():
    res = select(["docs/index.md", "LICENSE"])
    assert res["full"] is True
    for s in SUITES:
        assert res[s] is True


def test_empty_paths_full():
    res = select([])
    assert res["full"] is True
    for s in SUITES:
        assert res[s] is True


def test_backslash_path_normalised():
    res = select(["src\\x.py"])
    assert res["pytest"] is True
    assert res["pytest-postgres"] is True
    assert res["full"] is False
    assert res["rust"] is False


def test_rust_readme_docs_only():
    res = select(["rust/README.md"])
    assert res["full"] is False
    assert res["pytest"] is False
    assert res["pytest-postgres"] is False
    assert res["rust"] is False
    assert res["rust-worker"] is False
    assert res["rust-spike"] is False


def test_gate_all_selected_success():
    needs = {
        "select": {
            "result": "success",
            "outputs": {
                "pytest": "true",
                "pytest-postgres": "true",
                "rust": "true",
                "rust-worker": "true",
                "rust-spike": "true",
            },
        },
        "scrub": {"result": "success"},
        "file-size": {"result": "success"},
        "new-code-lang": {"result": "success"},
        "changelog-docs": {"result": "success"},
        "pytest": {"result": "success"},
        "pytest-postgres": {"result": "success"},
        "rust": {"result": "success"},
        "rust-worker": {"result": "success"},
        "rust-spike": {"result": "success"},
    }
    assert gate(needs) == []


def test_gate_selected_suite_skipped():
    needs = {
        "select": {
            "result": "success",
            "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "rust-spike": "false"},
        },
        "scrub": {"result": "skipped"},
        "file-size": {"result": "success"},
        "new-code-lang": {"result": "success"},
        "changelog-docs": {"result": "success"},
    }
    assert gate(needs) == ["scrub: skipped"]


def test_gate_selected_suite_failure():
    needs = {
        "select": {
            "result": "success",
            "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "rust-spike": "false"},
        },
        "scrub": {"result": "failure"},
        "file-size": {"result": "success"},
        "new-code-lang": {"result": "success"},
        "changelog-docs": {"result": "success"},
    }
    assert gate(needs) == ["scrub: failure"]


def test_gate_unselected_suite_skipped():
    needs = {
        "select": {
            "result": "success",
            "outputs": {
                "pytest": "false",
                "pytest-postgres": "false",
                "rust": "false",
                "rust-worker": "false",
                "rust-spike": "false",
            },
        },
        "scrub": {"result": "success"},
        "file-size": {"result": "success"},
        "new-code-lang": {"result": "success"},
        "changelog-docs": {"result": "success"},
        "pytest": {"result": "skipped"},
        "pytest-postgres": {"result": "skipped"},
    }
    assert gate(needs) == []


def test_gate_select_missing():
    needs = {}
    assert gate(needs) == ["select: missing"]


def test_gate_select_failure():
    needs = {
        "select": {"result": "failure"},
    }
    assert gate(needs) == ["select: failure"]


def test_gate_missing_output_value_counts_as_selected():
    needs = {
        "select": {
            "result": "success",
            "outputs": {},
        },
        "scrub": {"result": "success"},
        "file-size": {"result": "success"},
        "new-code-lang": {"result": "success"},
        "changelog-docs": {"result": "success"},
        "pytest": {"result": "failure"},
    }
    problems = gate(needs)
    assert "pytest: failure" in problems
    assert "rust: missing" in problems


def test_gate_docs_only_selection_passes():
    needs = {
        "select": {
            "result": "success",
            "outputs": {
                "pytest": "false",
                "pytest-postgres": "false",
                "rust": "false",
                "rust-worker": "false",
                "rust-spike": "false",
            },
        },
        "scrub": {"result": "success"},
        "file-size": {"result": "success"},
        "new-code-lang": {"result": "success"},
        "changelog-docs": {"result": "success"},
    }
    assert gate(needs) == []


def test_cli_select_files(capsys):
    ret = main(["select", "--files", "docs/a.md"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "pytest=false" in captured.out
    assert "scrub=" not in captured.out
    assert "full=false" in captured.out


def test_cli_select_github_output(monkeypatch, tmp_path):
    gh_out_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out_file))
    ret = main(["select", "--files", "docs/a.md"])
    assert ret == 0
    assert gh_out_file.exists()
    content = gh_out_file.read_text(encoding="utf-8")
    assert "pytest=false" in content
    assert "scrub=" not in content
    assert "full=false" in content


def test_cli_select_base_head_failure(monkeypatch, tmp_path, capsys):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    ret = main(["select", "--base", "nonexistent_sha_1", "--head", "nonexistent_sha_2"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "full=true" in captured.out
    for s in SUITES:
        assert f"{s}=true" in captured.out


def test_cli_gate_bad_json(capsys):
    ret = main(["gate", "--needs", "not-json"])
    assert ret == 1
    captured = capsys.readouterr()
    assert "ci-gate: bad needs json" in captured.err


def test_cli_gate_success(capsys):
    needs_json = (
        '{"select": {"result": "success", "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "rust-spike": "false"}}, '
        '"scrub": {"result": "success"}, "file-size": {"result": "success"}, "new-code-lang": {"result": "success"}, "changelog-docs": {"result": "success"}}'
    )
    ret = main(["gate", "--needs", needs_json])
    assert ret == 0
    captured = capsys.readouterr()
    assert "ci-gate: ok" in captured.out


def test_cli_gate_problems(capsys):
    needs_json = (
        '{"select": {"result": "success", "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "rust-spike": "false"}}, '
        '"scrub": {"result": "failure"}, "file-size": {"result": "success"}, "new-code-lang": {"result": "success"}, "changelog-docs": {"result": "success"}}'
    )
    ret = main(["gate", "--needs", needs_json])
    assert ret == 1
    captured = capsys.readouterr()
    assert "scrub: failure" in captured.err


@pytest.mark.parametrize("path", ["tests/test_x.py", "vendor/lib.js", "tools/other.py", "./src/a.py"])
def test_each_python_prefix_selects_pytest(path):
    res = select([path])
    assert res["pytest"] and res["pytest-postgres"] and not res["full"]
    assert not res["rust"]


def test_select_full_cli_runs_every_suite(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    # --full must win over an explicit docs-only file list (push/dispatch path)
    assert main(["select", "--full", "--files", "docs/a.md"]) == 0
    out = capsys.readouterr().out
    assert "full=true" in out and "rust-spike=true" in out and "false" not in out


def test_gate_cancelled_selected_suite_is_a_problem():
    outs = {s: "false" for s in SUITES}
    outs["pytest"] = "true"
    needs = {"select": {"result": "success", "outputs": outs}, "pytest": {"result": "cancelled"}}
    needs.update({j: {"result": "success"} for j in ci_select.ALWAYS})
    assert gate(needs) == ["pytest: cancelled"]


def test_fast_path_profiles_shards_and_failure_limit():
    import tomllib
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / 'rust/Cargo.toml').read_text())
    assert manifest['profile']['dev']['package']['*']['opt-level'] == 3
    assert manifest['profile']['dev']['package']['pg-worker-core']['opt-level'] == 3
    jobs = yaml.safe_load((root / '.github/workflows/tests.yml').read_text())['jobs']
    for name in ('pytest', 'pytest-postgres'):
        assert jobs[name]['strategy']['matrix']['shard'] == [0, 1, 2]
        assert jobs[name]['strategy']['fail-fast'] is True
        text = '\n'.join(step.get('run', '') for step in jobs[name]['steps'])
        assert '--dist loadgroup' in text and '--maxfail 10' in text
    assert 'cancel-on-failure' not in jobs


def test_three_shards_cover_only_affected_selection(tmp_path, shard_selector):
    import inspect
    import os
    import sys
    spec = importlib.util.spec_from_file_location('shard_affected', MODULE_PATH.with_name('affected_tests.py'))
    affected = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(affected)
    package = tmp_path / 'src/pkg'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'one.py').write_text('VALUE = 1\n')
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_selected.py').write_text('from pkg.one import VALUE\ndef test_selected():\n    assert VALUE == 1\n')
    (tests / 'test_unselected.py').write_text('raise RuntimeError("unaffected file must never be collected")\n')
    plan = affected.select_tests(tmp_path, ['src/pkg/one.py'])
    assert plan['tests'] == ['tests/test_selected.py']
    hooks = shard_selector.__globals__
    source = 'import os, sys, hashlib, pytest\nfrom pathlib import Path\n_PG = False\n'
    source += '\n'.join(inspect.getsource(hooks[name]) for name in (
        'shard_items', 'pytest_collection_modifyitems', 'pytest_sessionfinish') if name in hooks)
    source += '\ndef pytest_runtest_logreport(report):\n    if report.when == "call":\n        with Path(os.environ["SHARD_TRACE"]).open("a") as stream:\n            stream.write(report.nodeid + "\\n")\n'
    (tmp_path / 'conftest.py').write_text(source)
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    executed, empty = [], 0
    for index in range(3):
        trace = tmp_path / f'trace-{index}'
        env = dict(os.environ, PYTEST_SHARD=f'{index}/3', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                   PYTHONPATH=os.pathsep.join((str(tmp_path / 'src'), str(MODULE_PATH.parents[1] / 'tests'))), SHARD_TRACE=str(trace))
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-c', str(tmp_path / 'pytest.ini'),
            '--confcutdir', str(tmp_path), *plan['tests']], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        ran = trace.read_text().splitlines() if trace.exists() else []
        empty += not ran
        executed.extend(ran)
    assert empty == 2
    assert executed == ['tests/test_selected.py::test_selected']


@pytest.mark.parametrize('shard', ['0/3', '1/3', '2/3'])
def test_empty_affected_selection_never_invokes_default_collection(tmp_path, monkeypatch, capsys, shard):
    import sys
    spec = importlib.util.spec_from_file_location('empty_affected', MODULE_PATH.with_name('affected_tests.py'))
    affected = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(affected)
    monkeypatch.setenv('PYTEST_SHARD', shard)
    monkeypatch.setattr(affected, 'select_tests', lambda *a, **k: {'tests': []})
    monkeypatch.setattr(affected.subprocess, 'run', lambda *a, **k: pytest.fail('empty selection started pytest'))
    monkeypatch.setattr(sys, 'argv', ['affected_tests.py', '--base', 'HEAD', '--changed', 'docs/a.md', '--run'])
    assert affected.main(root=tmp_path) == 0
