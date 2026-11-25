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
DRAFT_LANE_SKIP = (
    "!(github.event_name == 'pull_request' && github.event.pull_request.draft"
    " && startsWith(github.head_ref, 'lane/'))"
)


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
    expr = re.sub(r"always\(\)", "True", expr)
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
    for event, head, draft, expected in CASES:
        got = {job: _runs(job, event, head, draft) for job in JOBS}
        assert set(got.values()) == {expected}, (event, head, draft, got)


def test_draft_lane_pr_skips_the_required_ci_gate_too():
    # A skipped required job reports success, so no check stays pending.
    assert not _runs("ci-gate", "pull_request", "lane/b", True)
    assert _runs("ci-gate", "pull_request", "land/train-1", True)
    # always() stays: the gate must still judge a skipped or failed needed job on every other run.
    assert JOBS["ci-gate"]["if"].startswith("${{ always() && ")


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
