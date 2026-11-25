"""Every tests.yml job has a distinct display name, and the name readers match it."""
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import merge_batch_build as build  # noqa: E402
import merge_batch_land as land  # noqa: E402
import merge_batch_util as util  # noqa: E402

WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
JOBS = WORKFLOW["jobs"]
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")


def _static(name: str) -> str:
    return EXPRESSION.sub("N", name)


def test_every_job_has_a_display_name():
    assert [job for job, body in JOBS.items() if not str(body.get("name", "")).strip()] == []


def test_display_names_are_unique():
    names = [_static(str(body["name"])) for body in JOBS.values()]
    assert len(names) == len(set(names))


def test_ci_gate_keeps_its_branch_protection_context():
    assert _render(JOBS["ci-gate"]["name"], action="synchronize") == "ci-gate"


def test_merge_batch_land_names_match_the_workflow():
    expected = {job: _render(body["name"], action="synchronize")
                for job, body in JOBS.items() if job not in land.SHARD_DISPLAY}
    assert land.JOB_DISPLAY == expected
    for suite, prefix in land.SHARD_DISPLAY.items():
        assert JOBS[suite]["name"].startswith(prefix + " (shard ")


def test_job_readers_accept_new_and_old_names():
    new = {land.JOB_DISPLAY[k] for k in land.JOB_DISPLAY}
    assert all(land.job_named(new, k) for k in land.JOB_DISPLAY)
    assert all(land.job_named({k}, k) for k in land.JOB_DISPLAY)
    shards = {f"{land.SHARD_DISPLAY['pytest']} (shard {i} of 4)" for i in (1, 2, 3, 4)}
    assert all(land.shard_named(shards, "pytest", i) for i in range(3))
    assert not land.shard_named(shards, "pytest-postgres", 0)
    assert land.shard_named({"pytest-postgres (1)"}, "pytest-postgres", 1)
    assert not land.shard_named({f"{land.SHARD_DISPLAY['pytest']} (shard 2 of 3)"}, "pytest", 0)


def _evaluate(expression, *, event="pull_request", action="", head="", draft=False, changes=(), run_id=42):
    """Evaluate the small subset of Actions expressions used by the workflow admission conditions."""
    expr = expression.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    expr = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", expr))
    expr = re.sub(r"(?:always|success)\(\)", "True", expr)
    draft_lane = event == "pull_request" and draft and head.startswith("lane/") and "base" not in changes
    metadata = event == "pull_request" and action == "edited" and "base" not in changes and (
        "title" in changes or "body" in changes)
    expr = expr.replace("needs.select.outputs.full", repr("" if draft_lane or metadata else "true"))
    values = {
        "event_name": event,
        "event.action": action,
        "event.changes.base": "base" in changes,
        "event.changes.body": "body" in changes,
        "event.changes.title": "title" in changes,
        "event.pull_request.draft": draft,
        "event.pull_request.title": "Example PR",
        "head_ref": head,
        "ref": "refs/pull/7/merge",
        "ref_name": "7/merge",
        "run_id": run_id,
        "workflow": "tests",
    }
    expr = re.sub(r"github\.([\w.]+)", lambda match: repr(values[match[1]]), expr)
    return eval(expr.strip(), {"format": str.format, "startsWith": str.startswith})  # noqa: S307


def _value(condition, **context):
    return _evaluate(EXPRESSION.fullmatch(condition)[1], **context)


def _render(template, **context):
    return EXPRESSION.sub(lambda match: str(_evaluate(match[1], **context)), template)


def _runs(job, event, head="", draft=False, **payload):
    """Evaluate a job's condition, with jobs needing select following select."""
    cond = JOBS[job].get("if")
    if cond is None:
        return True
    if "needs.select" in cond:
        return _runs("select", event, head, draft, **payload)
    return bool(_value(cond, event=event, head=head, draft=draft, **payload))


CASES = [
    ("pull_request", "lane/batch1-x", True, False),
    ("pull_request", "lane/batch1-x", False, True),
    ("pull_request", "land/train-20261124-33", False, True),
    ("pull_request", "land/train-20261124-33", True, True),
    ("pull_request", "feature/x", False, True),
    ("pull_request", "feature/x", True, True),
    ("pull_request", "batch/x", False, True),
    ("push", "", False, True),
    ("merge_group", "", False, True),
    ("workflow_dispatch", "", False, True),
    ("schedule", "", False, True),
]


def test_only_a_draft_lane_pull_request_runs_no_job():
    # The draft exception still skips every test job. The literal gate reports the witness.
    for event, head, draft, full in CASES:
        visible = {job for job in JOBS if _runs(job, event, head, draft)}
        assert visible == (set(JOBS) if full else {"ci-gate"}), (event, head, draft, visible)


def test_draft_lane_pr_skips_the_required_ci_gate_too():
    # The real Gate step stays skipped inside the literal check job.
    context = {"event": "pull_request", "action": "opened", "head": "lane/b", "draft": True}
    assert JOBS["ci-gate"]["if"] == "${{ always() }}"
    assert _render(JOBS["ci-gate"]["name"], **context) == "ci-gate"
    assert _render(WORKFLOW["run-name"], **context) == "Draft lane check 42"
    executed = [step.get("name", step.get("uses")) for step in JOBS["ci-gate"]["steps"]
                if "if" not in step or _value(step["if"], **context)]
    assert executed == ["Draft lane without test proof"]


def test_draft_lane_metadata_uses_the_policy_job_and_base_edits_use_the_real_gate():
    context = {"event": "pull_request", "action": "edited", "head": "lane/b", "draft": True}
    for changes in (("body",), ("title",)):
        assert _render(WORKFLOW["run-name"], **context, changes=changes) == "PR metadata check 42"
        assert _render(JOBS["ci-gate"]["name"], **context, changes=changes) == "ci-pr-description"
        assert {job for job in JOBS if _runs(job, **context, changes=changes)} == {"ci-gate"}
        steps = {step.get("name"): step for step in JOBS["ci-gate"]["steps"]}
        assert _value(steps["Commit trailers"]["if"], **context, changes=changes)
        assert not _value(steps["Draft lane without test proof"]["if"], **context, changes=changes)
    assert all(_runs(job, **context, changes=("body", "base")) for job in JOBS)
    assert _render(JOBS["ci-gate"]["name"], **context, changes=("base",)) == "ci-gate"
    assert _render(WORKFLOW["run-name"], **context, changes=("base",)) == "Tests: Example PR"


def test_body_and_title_edits_run_the_policy_check_in_isolated_concurrency():
    for changes in (("body",), ("title",), ("body", "title")):
        context = {"action": "edited", "changes": changes}
        assert {job for job in JOBS if _runs(job, "pull_request", **context)} == {"ci-gate"}
        assert _render(JOBS["ci-gate"]["name"], **context) == "ci-pr-description"
        assert _render(WORKFLOW["run-name"], **context) == "PR metadata check 42"
        concurrency = WORKFLOW["concurrency"]
        assert not _value(concurrency["cancel-in-progress"], **context)
        assert _render(concurrency["group"], **context) == "tests-refs/pull/7/merge-42"
        assert _render(concurrency["group"], run_id=43, **context) == "tests-refs/pull/7/merge-43"

        steps = {step.get("name"): step for step in JOBS["ci-gate"]["steps"]}
        assert not _value(steps["Gate"]["if"], **context)
        assert _value(steps["Commit trailers"]["if"], **context)


def test_base_edits_and_unclassified_events_keep_the_full_check_and_group():
    cases = [("edited", ("base",)), ("edited", ("body", "base")), ("edited", ("title", "base")),
             ("edited", ()), ("edited", ("other",)), ("synchronize", ("body",)), ("labeled", ())]
    for action, changes in cases:
        context = {"action": action, "changes": changes}
        assert all(_runs(job, "pull_request", **context) for job in JOBS)
        assert _render(JOBS["ci-gate"]["name"], **context) == "ci-gate"
        assert _render(WORKFLOW["run-name"], **context) != "PR metadata check 42"
        concurrency = WORKFLOW["concurrency"]
        assert _value(concurrency["cancel-in-progress"], **context) == (action != "labeled")
        assert _render(concurrency["group"], **context) == "tests-refs/pull/7/merge-pr"


def test_newest_full_ci_decides_even_after_a_verified_metadata_run(monkeypatch):
    head = "a" * 40
    run = {"head_sha": head, "head_branch": "batch/x", "event": "pull_request",
           "pull_requests": [{"number": 7}], "status": "completed", "conclusion": "success"}
    full = dict(run, id=40)
    metadata = dict(run, id=42, display_title="PR metadata check 42")
    runs = [metadata, full]
    full_jobs = [{"name": name, "status": "completed", "conclusion": "success"}
                 for name in (*land.JOB_DISPLAY.values(), *(
                     f"{prefix} (shard {i} of 3)" for prefix in land.SHARD_DISPLAY.values() for i in (1, 2, 3)
                 ))]
    jobs = {40: full_jobs, 42: [{"name": "ci-pr-description", "conclusion": "success"},
                                {"name": "Select suites for this change", "conclusion": "skipped"}]}

    def api(verb, path):
        if "/workflows/tests.yml/" in path:
            return {"workflow_runs": runs}
        run_jobs = jobs[int(path.split("/runs/", 1)[1].split("/", 1)[0])]
        return {"jobs": run_jobs, "total_count": len(run_jobs)}

    monkeypatch.setattr(land, "gh_json", api)
    monkeypatch.setattr(util, "gh_json", api)
    assert build.tests_runs("o/r", 7, head, api) == [full]
    assert land.require_full_run("o/r", head, "batch/x") == []
    assert util.ci_window("o/r", head)["ci_run_id"] == 40

    for state, conclusion in (("completed", "failure"), ("in_progress", None)):
        latest_full = dict(run, id=41, status=state, conclusion=conclusion)
        runs.insert(1, latest_full)
        with pytest.raises(land.MergeBatchError, match=r"tests run 41 is not green"):
            land.require_full_run("o/r", head, "batch/x")
        runs.remove(latest_full)

    # A legacy PR title can match the marker. A missing or skipped required gate cannot prove metadata.
    for mistaken_jobs in ([], full_jobs, [{"name": "ci-pr-description"},
                                          {"name": "ci-gate", "conclusion": "skipped"}]):
        jobs[42] = mistaken_jobs
        assert build.tests_runs("o/r", 7, head, api) == runs

    jobs[42] = [{"name": "ci-pr-description", "status": "in_progress"}]
    assert build.tests_runs("o/r", 7, head, api) == [full]


def test_description_policy_is_not_a_ci_rollup_verdict():
    description = {"__typename": "CheckRun", "workflowName": "tests", "name": "ci-pr-description",
                   "status": "COMPLETED", "conclusion": "FAILURE"}
    full_gate = dict(description, name="ci-gate")
    assert util.ci_state([description]) == "none"
    assert util.ci_state([description, dict(full_gate, conclusion="SUCCESS")]) == "green"
    assert util.ci_state([description, full_gate]) == "red"
    assert util.ci_state([dict(description, workflowName="another-workflow")]) == "red"


def test_draft_lane_never_supplies_actual_attempt_evidence(monkeypatch):
    head = "a" * 40
    draft = {"id": 42, "run_attempt": 2, "head_sha": head, "head_branch": "lane/x",
             "event": "pull_request", "pull_requests": [{"number": 7}],
             "status": "completed", "conclusion": "success", "display_title": "Draft lane check 42"}
    jobs = [{"name": "ci-gate", "status": "completed", "conclusion": "success",
             "steps": [{"name": "Draft lane without test proof", "status": "completed", "conclusion": "success"},
                       {"name": "Gate", "status": "completed", "conclusion": "skipped"}]},
            {"name": "Select suites for this change", "status": "completed", "conclusion": "skipped"}]
    endpoints = []

    def api(_verb, path):
        endpoints.append(path)
        if "/workflows/tests.yml/" in path:
            return {"workflow_runs": [draft]}
        return {"jobs": jobs, "total_count": len(jobs)}

    monkeypatch.setattr(build, "gh_json", api)
    monkeypatch.setattr(land, "gh_json", api)
    assert build.ci_attempts("o/r", 7, head) == []
    assert any("/runs/42/attempts/2/jobs" in path for path in endpoints)
    with pytest.raises(land.MergeBatchError, match="no tests run"):
        land.require_full_run("o/r", head, "lane/x")
    jobs[0]["steps"][0]["name"] = "Other step"
    with pytest.raises(land.MergeBatchError, match="draft lane marker"):
        build.ci_attempts("o/r", 7, head)


def test_ready_lane_rollup_does_not_reuse_an_earlier_draft_gate_as_test_proof():
    gate = {"__typename": "CheckRun", "workflowName": "tests", "name": "ci-gate",
            "status": "COMPLETED", "conclusion": "SUCCESS"}
    selector = dict(gate, name="Select suites for this change", conclusion="SKIPPED")
    assert util.ci_state([gate, selector], lane=True) == "none"
    assert util.ci_state([gate, dict(selector, conclusion="SUCCESS")], lane=True) == "green"
