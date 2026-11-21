"""LAND-MODE-TESTED-FF: the push run on main skips the heavy jobs for a commit that already passed full CI.

Mutations killed: a skip without the land App's authorization, one written by another identity, one whose
evidence run differs from the one `ci-full` names, a `ci-full` that tested another tree, a failed status read
as success, an unset creator id that skips, a skip on a pull request event, the gate refusing the fast-only
push run, and a fast-only run for a commit that has no evidence.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ci_select.py"
spec = importlib.util.spec_from_file_location("ci_select_tested", MODULE_PATH)
ci_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_select)

TREE = "a" * 40
APP = 4242


def status(context, state, description, creator=APP, sid=1):
    return {"id": sid, "context": context, "state": state, "description": description, "creator": {"id": creator}}


def good(tree=TREE):
    return [status("land-auth", "success", "attempt a1 evidence run 77 attempt 2", sid=2),
            status("ci-full", "success", f"run 77 attempt 2 tree {tree}", creator=1, sid=3)]


def test_authorization_plus_matching_full_evidence_is_a_tested_commit():
    assert ci_select.landing_evidence(good(), TREE, APP) == "77 attempt 2"


@pytest.mark.parametrize("case", [
    "no_statuses", "no_creator_id", "other_creator", "auth_failure", "auth_pending", "other_run", "other_attempt",
    "other_tree", "ci_full_failed", "no_ci_full", "bad_description", "newer_auth_failure",
])
def test_anything_else_runs_the_full_suite(case):
    statuses = good()
    app = APP
    if case == "no_statuses":
        statuses = []
    elif case == "no_creator_id":
        app = None
    elif case == "other_creator":
        statuses[0]["creator"]["id"] = 7
    elif case == "auth_failure":
        statuses[0]["state"] = "failure"
    elif case == "auth_pending":
        statuses[0]["state"] = "pending"
    elif case == "other_run":
        statuses[1]["description"] = f"run 78 attempt 2 tree {TREE}"
    elif case == "other_attempt":
        statuses[1]["description"] = f"run 77 attempt 1 tree {TREE}"
    elif case == "other_tree":
        statuses[1]["description"] = f"run 77 attempt 2 tree {'b' * 40}"
    elif case == "ci_full_failed":
        statuses[1]["state"] = "failure"
    elif case == "no_ci_full":
        statuses = statuses[:1]
    elif case == "bad_description":
        statuses[0]["description"] = "approved"
    elif case == "newer_auth_failure":
        statuses.append(status("land-auth", "failure", "withdrawn", sid=9))
    assert ci_select.landing_evidence(statuses, TREE, app) is None


def run_select(tmp_path, capsys, event, statuses, app=APP, tree=TREE):
    path = tmp_path / "statuses.json"
    path.write_text(json.dumps(statuses))
    argv = ["select", "--event", event, "--statuses", str(path), "--tree", tree]
    if app is not None:
        argv += ["--land-creator-id", str(app)]
    assert ci_select.main(argv) == 0
    return dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())


def test_a_push_of_a_tested_commit_selects_no_heavy_job(tmp_path, capsys):
    out = run_select(tmp_path, capsys, "push", good())
    assert {out[s] for s in ci_select.SUITES} == {"false"}
    assert out["full"] == "false" and out["tested"] == "true" and out["evidence"] == "77 attempt 2"


def test_a_push_of_any_other_commit_runs_everything(tmp_path, capsys):
    out = run_select(tmp_path, capsys, "push", good(tree="b" * 40))
    assert {out[s] for s in ci_select.SUITES} == {"true"} and out["full"] == "true" and "tested" not in out


def test_a_pull_request_event_never_skips_on_evidence(tmp_path, capsys):
    out = run_select(tmp_path, capsys, "pull_request", good())
    assert "tested" not in out


def test_the_gate_accepts_the_fast_only_push_run_and_still_demands_the_fast_jobs():
    outs = {s: "false" for s in ci_select.SUITES} | {"full": "false", "tested": "true"}
    needs = {"select": {"result": "success", "outputs": outs}}
    needs.update({j: {"result": "success"} for j in ci_select.ALWAYS})
    assert ci_select.gate(needs, require_full=True) == []
    needs["scrub"] = {"result": "failure"}
    assert ci_select.gate(needs, require_full=True) == ["scrub: failure"]
    outs.pop("tested")
    needs["scrub"] = {"result": "success"}
    assert "select: merge candidate ran the affected selection, not the full suite" in ci_select.gate(needs, True)


def test_ci_full_is_posted_only_when_the_tested_tree_is_the_head_tree():
    assert ci_select.ci_full_description(77, 2, TREE, TREE) == f"run 77 attempt 2 tree {TREE}"
    assert ci_select.ci_full_description(77, 2, TREE, "b" * 40) is None
    assert ci_select.landing_evidence(
        [status("land-auth", "success", "attempt a evidence run 77 attempt 2"),
         status("ci-full", "success", ci_select.ci_full_description(77, 2, TREE, TREE), creator=1, sid=3)],
        TREE, APP) == "77 attempt 2"
