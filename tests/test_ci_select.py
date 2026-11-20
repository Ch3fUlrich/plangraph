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
                "browser": "true",
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
        "browser": {"result": "success"},
        "rust-windows": {"result": "success"},
    }
    assert gate(needs) == []


def test_gate_selected_suite_skipped():
    needs = {
        "select": {
            "result": "success",
            "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "browser": "false", "rust-spike": "false"},
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
            "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "browser": "false", "rust-spike": "false"},
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
                "browser": "false", "rust-spike": "false",
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
                "browser": "false", "rust-spike": "false",
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
        '{"select": {"result": "success", "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "browser": "false", "rust-spike": "false"}}, '
        '"scrub": {"result": "success"}, "file-size": {"result": "success"}, "new-code-lang": {"result": "success"}, "changelog-docs": {"result": "success"}}'
    )
    ret = main(["gate", "--needs", needs_json])
    assert ret == 0
    captured = capsys.readouterr()
    assert "ci-gate: ok" in captured.out


def test_cli_gate_problems(capsys):
    needs_json = (
        '{"select": {"result": "success", "outputs": {"pytest": "false", "pytest-postgres": "false", "rust": "false", "rust-worker": "false", "browser": "false", "rust-spike": "false"}}, '
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


# CI-FULL-RUN-ONCE-BEFORE-MERGE: the full suite runs only for the merge candidate.
full_run_required = ci_select.full_run_required


@pytest.mark.parametrize("event", ["opened", "ready_for_review", "synchronize"])
def test_ready_or_draft_lane_pr_to_main_gets_the_selection(event):
    assert not full_run_required("pull_request", "main", "lane/some-item", [])


def test_batch_head_gets_the_full_suite():
    assert full_run_required("pull_request", "main", "batch/batch9", [])


def test_go_labelled_head_gets_the_full_suite():
    assert full_run_required("pull_request", "main", "lane/some-item", ["batch-ready", ci_select.FULL_LABEL])


def test_label_does_not_matter_off_main_and_stacked_batch_is_selection():
    assert not full_run_required("pull_request", "lane/base", "lane/x", [ci_select.FULL_LABEL])
    assert not full_run_required("pull_request", "lane/base", "batch/x", [])


@pytest.mark.parametrize("event", ["push", "merge_group", "workflow_dispatch", "schedule"])
def test_record_and_queue_events_run_full(event):
    assert full_run_required(event)


def _selection_only_needs():
    outs = {s: "false" for s in SUITES} | {"full": "false", "pytest": "true"}
    needs = {"select": {"result": "success", "outputs": outs}, "pytest": {"result": "success"}}
    needs.update({j: {"result": "success"} for j in ci_select.ALWAYS})
    return needs


def test_gate_refuses_a_candidate_that_ran_only_the_selection():
    needs = _selection_only_needs()
    assert gate(needs) == []
    assert gate(needs, require_full=True) == [
        "select: merge candidate ran the affected selection, not the full suite"]


def test_cli_select_event_picks_full_for_batch_head_and_selection_for_lane(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = ["select", "--event", "pull_request", "--base-ref", "main", "--labels", "[]",
            "--files", "docs/a.md"]
    assert main(args + ["--head-ref", "batch/x"]) == 0
    assert "full=true" in capsys.readouterr().out
    assert main(args + ["--head-ref", "lane/x"]) == 0
    assert "full=true" not in capsys.readouterr().out


def test_cli_gate_event_requires_full_for_a_go_labelled_head(capsys):
    import json
    needs = json.dumps(_selection_only_needs())
    base = ["gate", "--needs", needs, "--event", "pull_request", "--base-ref", "main", "--head-ref", "lane/x"]
    assert main(base + ["--labels", "[]"]) == 0
    assert main(base + ["--labels", '["ci-full"]']) == 1
    assert "not the full suite" in capsys.readouterr().err


def test_cli_rejects_non_list_labels(capsys):
    assert main(["select", "--event", "pull_request", "--labels", "{}"]) == 1


def test_workflow_routes_the_decision_through_ci_select():
    import yaml
    wf = yaml.safe_load((MODULE_PATH.parents[1] / ".github" / "workflows" / "tests.yml").read_text())
    triggers = wf[True] if True in wf else wf["on"]
    assert "labeled" in triggers["pull_request"]["types"] and "merge_group" in triggers
    step = [s for s in wf["jobs"]["select"]["steps"] if s.get("id") == "sel"][0]
    assert "--event" in step["run"] and "draft" not in step["run"]
    assert "--event" in [s for s in wf["jobs"]["ci-gate"]["steps"] if s.get("name") == "Gate"][0]["run"]


def test_undraft_starts_no_run_and_a_label_run_never_cancels_a_running_one():
    import yaml
    wf = yaml.safe_load((MODULE_PATH.parents[1] / ".github" / "workflows" / "tests.yml").read_text())
    triggers = wf[True] if True in wf else wf["on"]
    assert "ready_for_review" not in triggers["pull_request"]["types"]
    assert "synchronize" in triggers["pull_request"]["types"]
    cancel = wf["concurrency"]["cancel-in-progress"]
    assert "github.event.action != 'labeled'" in cancel
