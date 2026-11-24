"""Every tests.yml job has a distinct display name, and the name readers match it."""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import merge_batch_land as land  # noqa: E402

JOBS = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]


def _static(name: str) -> str:
    return re.sub(r"\$\{\{.*?\}\}", "N", name)


def test_every_job_has_a_display_name():
    assert [job for job, body in JOBS.items() if not str(body.get("name", "")).strip()] == []


def test_display_names_are_unique():
    names = [_static(str(body["name"])) for body in JOBS.values()]
    assert len(names) == len(set(names))


def test_ci_gate_keeps_its_branch_protection_context():
    assert JOBS["ci-gate"]["name"] == "ci-gate"


def test_merge_batch_land_names_match_the_workflow():
    expected = {job: body["name"] for job, body in JOBS.items() if job not in land.SHARD_DISPLAY}
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


# A draft lane/* PR (a batch part) runs no job: the root jobs and ci-gate carry one condition.
def _runs(job: str, event: str, head_ref: str = "", draft: bool = False) -> bool:
    """Evaluate a job's `if:` for an event; jobs needing select follow select."""
    body = JOBS[job]
    cond = str(body.get("if", "true")).strip()
    if "needs.select" in cond:
        return _runs("select", event, head_ref, draft)
    expr = re.sub(r"^\$\{\{(.*)\}\}$", r"\1", cond).strip()
    expr = expr.replace("&&", " and ").replace("||", " or ").replace("!(", " not (")
    expr = re.sub(r"always\(\)", "True", expr)
    expr = re.sub(r"startsWith\(([\w.]+), '([^']*)'\)", r"\1.startswith('\2')", expr)
    expr = expr.replace("github.event_name", "ev").replace("github.event.pull_request.draft", "draft")
    expr = expr.replace("github.head_ref", "head")
    return bool(eval(expr, {"ev": event, "draft": draft, "head": head_ref}))  # noqa: S307


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
