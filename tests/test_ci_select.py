from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from test_ci_job_names import _runs
import json
import shutil
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


def test_shared_rust_python_helper_selects_both_workspaces():
    res = select(["rust/test_support/python.rs"])
    assert res["rust"] and res["rust-worker"] and res["pytest"]
    assert not res["full"]


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
        assert "needs" not in wf["jobs"][job]
        assert not _runs(job, "pull_request", "lane/batch-part", draft=True, action="synchronize")
        assert _runs(job, "pull_request", "lane/batch-part", action="synchronize")
        assert not _runs(job, "pull_request", action="edited", changes=("body",))
        assert _runs(job, "pull_request", action="edited", changes=("body", "base"))
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


def test_ci_github_workflow_full(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert main(["select", "--event", "pull_request", "--base-ref", "main",
                 "--head-ref", "lane/workflows", "--labels", '["ci-full"]',
                 "--files", ".github/workflows/tests.yml"]) == 0
    output = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert output == dict.fromkeys((*SUITES, "full"), "true")


def test_ci_only_workflows_select_regressions():
    res = select([".github/workflows/tests.yml", ".github/workflows/release.yml"])
    assert {job for job, active in res.items() if active} == {"pytest", "rust"}
    assert select([".github/workflows/tests.yml", "src/m.py"])["full"] is True


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


def test_ready_or_draft_lane_pr_to_main_gets_the_selection():
    assert not full_run_required("pull_request", "main", "lane/some-item", [])


def test_batch_head_gets_the_affected_selection():
    assert not full_run_required("pull_request", "main", "batch/batch9", [])


def test_go_labelled_head_gets_the_full_suite():
    assert full_run_required("pull_request", "main", "lane/some-item", ["batch-ready", ci_select.FULL_LABEL])


def test_label_does_not_matter_off_main_and_stacked_batch_is_selection():
    assert not full_run_required("pull_request", "lane/base", "lane/x", [ci_select.FULL_LABEL])
    assert not full_run_required("pull_request", "lane/base", "batch/x", [])


@pytest.mark.parametrize("event", ["push", "merge_group", "schedule"])
def test_push_queue_and_schedule_events_run_the_selection(event):
    assert not full_run_required(event)


def test_a_manual_dispatch_runs_full():
    assert full_run_required("workflow_dispatch")


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


def test_cli_select_event_picks_full_for_train_head_and_selection_for_batch_and_lane(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = ["select", "--event", "pull_request", "--base-ref", "main", "--labels", "[]",
            "--files", ".github/workflows/tests.yml"]
    assert main(args + ["--head-ref", "land/train-x"]) == 0
    assert "full=true" in capsys.readouterr().out
    for head in ("batch/x", "lane/x"):
        assert main(args + ["--head-ref", head]) == 0
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
    assert "schedule" not in triggers   # a nightly run would be a second full run
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


# LAND-PART-ENTRY-CHECKS: a part runs the affected selection, the train and the merge candidate run everything.
def test_a_labelled_part_on_a_batch_head_runs_the_affected_selection():
    assert not full_run_required("pull_request", "main", "batch/part-x", [ci_select.PART_LABEL])
    assert full_run_required("pull_request", "main", "batch/batch9", [ci_select.FULL_LABEL])  # labelled at merge


def test_a_train_runs_everything_whatever_its_labels():
    assert full_run_required("pull_request", "main", "land/train-20261121-3", [])
    assert full_run_required("pull_request", "main", "land/train-20261121-3", [ci_select.PART_LABEL])
    assert not full_run_required("pull_request", "lane/base", "land/train-x", [])


def test_a_part_stays_affected_only_even_with_a_stray_full_label_but_never_a_train():
    labels = [ci_select.PART_LABEL, ci_select.FULL_LABEL]
    assert not full_run_required("pull_request", "main", "batch/part-x", labels)
    assert not full_run_required("pull_request", "main", "lane/x", labels)
    assert full_run_required("pull_request", "main", "land/train-1", labels)


def test_a_part_run_is_selection_in_the_gate_and_a_train_run_must_be_full(capsys):
    needs = json.dumps(_selection_only_needs())
    base = ["gate", "--needs", needs, "--event", "pull_request", "--base-ref", "main"]
    assert ci_select.main(base + ["--head-ref", "batch/part-x", "--labels", '["land-part"]']) == 0
    assert ci_select.main(base + ["--head-ref", "land/train-1", "--labels", '["land-part"]']) == 1
    assert "merge candidate ran the affected selection" in capsys.readouterr().err


@pytest.mark.parametrize("path", [
    "src/plangraph/planner/provider_stop.py", "src/plangraph/planner/measured_down.py",
    "src/plangraph/routing_support/liveness.py"])
def test_a_python_only_quota_change_selects_rust_and_so_the_windows_job(path):
    assert ci_select.select([path])["rust"] is True


def test_the_windows_job_runs_whenever_rust_is_selected():
    import yaml
    jobs = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml").read_text())["jobs"]
    assert jobs["rust-windows"]["if"] == "needs.select.outputs.rust == 'true'"


# CI-RUN-ONCE-AT-MERGE: every event but the merge candidate selects from its own diff, Rust by crate.
def _repo(root, base_files, files):
    """A repository with base_files committed, then files on top: (root, base sha, head sha)."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                               *args], check=True, capture_output=True, text=True).stdout.strip()
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main")
    shas = []
    for step in (base_files, files):
        for name, text in step.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        git("add", "-A")
        git("commit", "-qm", "step")
        shas.append(git("rev-parse", "HEAD"))
    return root, *shas


def _select_out(capsys, monkeypatch, root, *argv):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.chdir(root)
    assert main(["select", *argv]) == 0
    return dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())


def _workflow():
    import yaml
    return yaml.safe_load((MODULE_PATH.parents[1] / ".github/workflows/tests.yml").read_text())


CRATES = {
    "rust/Cargo.toml": '[workspace]\nmembers = ["a", "b", "c"]\nresolver = "2"\n',
    "rust/a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n',
    "rust/a/src/lib.rs": "",
    "rust/b/Cargo.toml": '[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n[dependencies]\na = { path = "../a" }\n',
    "rust/b/src/lib.rs": "",
    "rust/c/Cargo.toml": '[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n',
    "rust/c/src/lib.rs": "",
    "src/plangraph/__init__.py": "", "src/plangraph/thing.py": "X = 1\n", "docs/x.md": "x\n",
}
PR = ["--event", "pull_request", "--base-ref", "main", "--labels", "[]"]


def test_a_pull_request_touching_one_module_runs_the_affected_tests_not_the_full_suite(tmp_path, capsys, monkeypatch):
    root, base, head = _repo(tmp_path, CRATES, {"src/plangraph/thing.py": "X = 2\n"})
    out = _select_out(capsys, monkeypatch, root, *PR, "--head-ref", "lane/x", "--base", base, "--head", head)
    assert out["full"] == "false" and out["pytest"] == "true" and out["rust"] == "false"
    jobs = _workflow()["jobs"]
    for job, name in (("pytest", "pytest"), ("pytest-postgres", "pytest (contract suite + keeper storage tests on Postgres)")):
        step = next(s for s in jobs[job]["steps"] if s.get("name") == name)
        command = step["with"]["command"]
        # Every non-full event, a push included, runs the file-level affected selection from the select base.
        assert 'if [ "${{ needs.select.outputs.full }}" != "true" ]; then' in command
        assert "github.event_name" not in command
        assert 'python tools/affected_tests.py --base "$BASE_SHA"' in command
        assert step["env"]["BASE_SHA"] == "${{ needs.select.outputs.base }}"


def test_a_batch_head_is_not_full_and_a_train_head_into_main_is(tmp_path, capsys, monkeypatch):
    root, base, head = _repo(tmp_path, CRATES, {"src/plangraph/thing.py": "X = 2\n"})
    diff = ["--base", base, "--head", head]
    assert _select_out(capsys, monkeypatch, root, *PR, *diff, "--head-ref", "batch/b1")["full"] == "false"
    assert _select_out(capsys, monkeypatch, root, *PR, *diff, "--head-ref", "land/train-20261128-1")["full"] == "true"


def test_a_push_to_main_is_not_full_and_selects_from_before_to_after(tmp_path, capsys, monkeypatch):
    root, before, after = _repo(tmp_path, CRATES, {"docs/x.md": "y\n"})
    out = _select_out(capsys, monkeypatch, root, "--event", "push", "--base", before, "--head", after)
    assert out["full"] == "false" and not any(out[s] == "true" for s in SUITES)
    root, before, after = _repo(tmp_path / "py", CRATES, {"src/plangraph/thing.py": "X = 2\n"})
    out = _select_out(capsys, monkeypatch, root, "--event", "push", "--base", before, "--head", after)
    assert out["full"] == "false" and out["pytest"] == "true" and out["rust"] == "false"
    step = next(s for s in _workflow()["jobs"]["select"]["steps"] if s.get("id") == "sel")
    assert step["env"]["BASE_SHA"].endswith("|| github.event.before }}")
    assert step["env"]["HEAD_SHA"].endswith("|| github.sha }}")
    assert 'echo "base=$BASE_SHA" >> "$GITHUB_OUTPUT"' in step["run"]


@pytest.mark.skipif(shutil.which("cargo") is None, reason="the crate picker needs cargo on PATH")
def test_a_change_in_one_crate_selects_that_crate_and_its_dependents_only(tmp_path, capsys, monkeypatch):
    root, base, head = _repo(tmp_path, CRATES, {"rust/a/src/lib.rs": "// x\n"})
    out = _select_out(capsys, monkeypatch, root, *PR, "--head-ref", "lane/x", "--base", base, "--head", head)
    assert out["rust"] == "true" and out["full"] == "false"
    assert out["rust-packages"] == "a b"


@pytest.mark.skipif(shutil.which("cargo") is None, reason="the crate picker needs cargo on PATH")
def test_a_rust_input_outside_rust_or_a_full_run_tests_the_whole_workspace(tmp_path, capsys, monkeypatch):
    root, base, head = _repo(tmp_path, {**CRATES, "src/plangraph/settings.py": ""},
                             {"rust/a/src/lib.rs": "// x\n", "src/plangraph/settings.py": "# x\n"})
    diff = ["--base", base, "--head", head]
    out = _select_out(capsys, monkeypatch, root, *PR, *diff, "--head-ref", "lane/x")
    assert out["rust"] == "true" and "rust-packages" not in out
    out = _select_out(capsys, monkeypatch, root, *PR, *diff, "--head-ref", "land/train-1")
    assert out["full"] == "true" and "rust-packages" not in out


def test_rust_jobs_test_the_selected_crates_and_the_whole_workspace_without_a_list():
    jobs = _workflow()["jobs"]
    assert jobs["select"]["outputs"]["rust-packages"] == "${{ steps.sel.outputs.rust-packages }}"
    steps = {s.get("name"): s for s in jobs["rust"]["steps"]}
    for name, cargo in (("cargo clippy", 'cargo clippy "${packages[@]}" --all-targets'),
                        ("cargo test", 'cargo test "${packages[@]}"')):
        assert steps[name]["env"]["RUST_PACKAGES"] == "${{ needs.select.outputs.rust-packages }}"
        assert cargo in steps[name]["run"]
        script = steps[name]["run"].splitlines()[0] + '\nprintf "%s|" "${packages[@]}"'
        for listed, expected in (("", "|"), ("a b", "-p|a|-p|b|")):
            got = subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", "RUST_PACKAGES": listed},
                                 capture_output=True, text=True, check=True).stdout
            assert got == expected, (listed, got)
    windows = {s.get("name"): s for s in jobs["rust-windows"]["steps"]}
    for crate in ("pg-spawnd", "pg-worker-core"):
        cond = windows[f"cargo test -p {crate}"]["if"]
        assert cond.startswith("needs.select.outputs.rust-packages == '' ||") and f"' {crate} '" in cond
