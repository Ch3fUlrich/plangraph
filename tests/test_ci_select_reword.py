"""LE-CI-TRAIN-TREE-EVIDENCE: a reworded train commit reuses the green required run of its old head.

Mutations killed: a reuse for another tree, a reuse of a run that was not the required workflow on the old head,
a run that did not finish green, a newer attempt (red or not) than the one `ci-full` names, a run or attempt
different from the status, a status that is not success, an unchanged head read as a reword, and a reuse the
gate or the select output would not report.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ci_select.py"
spec = importlib.util.spec_from_file_location("ci_select_reword", MODULE_PATH)
ci_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_select)

OLD, NEW, TREE = "1" * 40, "2" * 40, "a" * 40
BOT = {"login": "github-actions[bot]"}

def full_jobs(tier="self", **over):
    """The jobs of a full run of the latest attempt on a runner tier, as the protected matrix lists them, all green."""
    matrix = tomllib.loads((MODULE_PATH.parents[1] / "deploy/ci-watch/matrix.toml").read_text())
    return [{"name": n, "status": "completed", "conclusion": "success"} | over.get(n, {}) for n in matrix[tier]]


def statuses(tree=TREE, state="success", run=77, attempt=2):
    return [{"id": 3, "context": "ci-full", "state": state, "creator": BOT,
             "description": f"run {run} attempt {attempt} tree {tree}"}]


def run_record(**over):
    record = {"id": 77, "run_attempt": 2, "conclusion": "success", "head_sha": OLD, "path": ".github/workflows/tests.yml",
              "event": "pull_request"}
    return record | over


def reuse(stats=None, run=None, old=OLD, new=NEW, old_tree=TREE, new_tree=TREE, jobs=None):
    return ci_select.reword_evidence(statuses() if stats is None else stats, run_record() if run is None else run,
                                     old, new, old_tree, new_tree, full_jobs() if jobs is None else jobs)


@pytest.mark.parametrize("tier", ["self", "hosted"])
def test_a_green_full_run_of_either_runner_tier_is_reused(tier):
    assert reuse(jobs=full_jobs(tier)) is not None


def test_a_green_required_run_on_a_tree_equal_commit_is_reused_and_bound_to_both_commits():
    assert reuse() == f"run 77 attempt 2 tree {TREE} reused from {OLD} for {NEW}"


@pytest.mark.parametrize("case", [
    "other_tree", "no_new_tree", "same_commit", "run_missing", "unverified_head", "other_workflow", "other_event",
    "red_latest", "newer_attempt", "other_run", "other_attempt", "status_red", "status_missing", "status_tree",
    "other_publisher", "no_publisher", "affected_only_jobs", "red_job", "skipped_job", "missing_shard", "no_jobs",
    "missing_fourth_shard", "hosted_missing_shard", "build_missing",
])
def test_anything_else_runs_the_suite_again(case):
    kwargs = {}
    if case == "other_tree":
        kwargs["new_tree"] = "b" * 40
    elif case == "no_new_tree":
        kwargs["new_tree"] = ""
        kwargs["old_tree"] = ""
    elif case == "same_commit":
        kwargs["new"] = OLD
    elif case == "run_missing":
        kwargs["run"] = {}
    elif case == "unverified_head":
        kwargs["run"] = run_record(head_sha=NEW)
    elif case == "other_workflow":
        kwargs["run"] = run_record(path=".github/workflows/other.yml")
    elif case == "other_event":
        kwargs["run"] = run_record(event="workflow_dispatch")
    elif case == "red_latest":
        kwargs["run"] = run_record(conclusion="failure")
    elif case == "newer_attempt":
        kwargs["run"] = run_record(run_attempt=3)
    elif case == "other_run":
        kwargs["run"] = run_record(id=78)
    elif case == "other_attempt":
        kwargs["stats"] = statuses(attempt=1)
    elif case == "status_red":
        kwargs["stats"] = statuses(state="failure")
    elif case == "status_missing":
        kwargs["stats"] = []
    elif case == "status_tree":
        kwargs["stats"] = statuses(tree="c" * 40)
    elif case in ("other_publisher", "no_publisher"):
        stats = statuses()
        stats[0]["creator"] = {"login": "someone"} if case == "other_publisher" else None
        kwargs["stats"] = stats
    elif case == "affected_only_jobs":   # a green affected-only run: the heavy jobs never ran
        kwargs["jobs"] = [j for j in full_jobs() if not j["name"].startswith(("Python tests", "Rust", "UI tests"))]
    elif case == "red_job":
        kwargs["jobs"] = full_jobs(**{"Rust worker crate: fmt, clippy, tests": {"conclusion": "failure"}})
    elif case == "skipped_job":
        kwargs["jobs"] = full_jobs(**{"Rust spawner and worker core tests on Windows": {"conclusion": "skipped"}})
    elif case == "missing_shard":
        kwargs["jobs"] = [j for j in full_jobs() if "(shard 3 of 4)" not in j["name"]]
    elif case == "no_jobs":
        kwargs["jobs"] = []
    elif case == "missing_fourth_shard":   # a self-hosted run whose fourth SQLite shard never reported
        kwargs["jobs"] = [j for j in full_jobs() if "(shard 4 of 4)" not in j["name"]]
    elif case == "hosted_missing_shard":
        kwargs["jobs"] = [j for j in full_jobs("hosted") if "(shard 3 of 3)" not in j["name"] or "SQLite" not in j["name"]]
    elif case == "build_missing":
        kwargs["jobs"] = [j for j in full_jobs() if not j["name"].startswith("Build pg-spawnd")]
    assert reuse(**kwargs) is None


def test_the_select_output_of_a_reword_says_tested_keeps_full_for_the_go_verifier_and_names_the_reuse(tmp_path, capsys):
    stat = tmp_path / "statuses.json"
    stat.write_text(json.dumps(statuses()))
    run = tmp_path / "run.json"
    run.write_text('{"id": 77, "run_attempt": 2, "conclusion": "success", "head_sha": "%s", "path": ".github/workflows/tests.yml", "event": "pull_request"}' % OLD)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": full_jobs()}))
    code = ci_select.main(["select", "--event", "pull_request", "--base-ref", "main", "--head-ref", "land/train-1",
                           "--labels", "[]", "--base", "x", "--head", "y", "--statuses", str(stat), "--tree", TREE,
                           "--old-head", OLD, "--new-head", NEW, "--old-tree", TREE, "--old-run", str(run),
                           "--old-jobs", str(jobs)])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert {"tested=true", "full=true", "pytest=false", "rust=false", f"evidence=run 77 attempt 2 tree {TREE} reused from {OLD} for {NEW}"} <= set(out)


def test_the_gate_accepts_a_reuse_that_skipped_every_suite_but_still_needs_the_always_jobs():
    suites = {s: {"result": "skipped"} for s in ci_select.SUITES} | {"rust-windows": {"result": "skipped"}}
    always = {s: {"result": "success"} for s in ci_select.ALWAYS}
    outputs = {"full": "true", "tested": "true", **{s: "false" for s in ci_select.SUITES}}
    needs = {"select": {"result": "success", "outputs": outputs}} | suites | always
    assert ci_select.gate(needs, require_full=True) == []
    needs["scrub"] = {"result": "failure"}
    assert ci_select.gate(needs, require_full=True) == ["scrub: failure"]
    # Without the reuse a full run with skipped suites is refused, as before.
    outputs.pop("tested")
    needs["scrub"] = {"result": "success"}
    assert "pytest: skipped" in ci_select.gate(needs, require_full=True)
