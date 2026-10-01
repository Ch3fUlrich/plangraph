"""Tests for the run-record, semaphore-task and decision verifiers.

Fakes live in ``evidence_fakes``; the real adapters are covered in
``test_evidence_adapters.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from evidence_fakes import ITEM, FakeDecisions, FakeRuns, FakeSemaphore, make_ctx, verify
from plangraph.keeper import evidence as ev


# --------------------------------------------------------------------------- #
# run-record
# --------------------------------------------------------------------------- #

def test_run_record_pass_title_contains_key():
    ctx = make_ctx(runs=FakeRuns(job={"request": {"title": "x " + ITEM["key"] + " y"}, "task": ""}))
    assert verify("run-record", {"run_id": "run-1"}, ctx).ok is True


def test_run_record_pass_task_contains_id():
    ctx = make_ctx(runs=FakeRuns(job={"request": {"title": "other"}, "task": "do " + ITEM["id"] + " now"}))
    assert verify("run-record", {"run_id": "run-1"}, ctx).ok is True


def test_run_record_missing_run_id():
    res = verify("run-record", {}, make_ctx())
    assert res.ok is False
    assert "run_id" in res.detail


def test_run_record_bad_run_id_traversal():
    ctx = make_ctx(runs=FakeRuns(valid=False))
    res = verify("run-record", {"run_id": "../../etc/passwd"}, ctx)
    assert res.ok is False
    assert "run id" in res.detail.lower() or "invalid" in res.detail.lower()


def test_run_record_for_another_item():
    ctx = make_ctx(runs=FakeRuns(job={"request": {"title": "routing:OTHER"}, "task": "nope"}))
    assert verify("run-record", {"run_id": "run-1"}, ctx).ok is False


def test_run_record_rc_nonzero():
    ctx = make_ctx(runs=FakeRuns(exit={"rc": 1}))
    res = verify("run-record", {"run_id": "run-1"}, ctx)
    assert res.ok is False
    assert "rc" in res.detail.lower()


def test_run_record_missing_job_file():
    ctx = make_ctx(runs=FakeRuns(job_error=FileNotFoundError("no job.json")))
    assert verify("run-record", {"run_id": "run-1"}, ctx).ok is False


def test_run_record_missing_exit_file():
    ctx = make_ctx(runs=FakeRuns(exit_error=FileNotFoundError("no exit.json")))
    assert verify("run-record", {"run_id": "run-1"}, ctx).ok is False


# --------------------------------------------------------------------------- #
# semaphore-task
# --------------------------------------------------------------------------- #

def test_semaphore_pass():
    assert verify("semaphore-task", {"task_id": "t-1"}, make_ctx()).ok is True


def test_semaphore_pass_template_id():
    ctx = make_ctx(semaphore=FakeSemaphore({"status": "success", "template": {"id": 7, "name": "x"}}))
    assert verify("semaphore-task", {"task_id": "t-1", "template": 7}, ctx).ok is True


def test_semaphore_pass_template_name():
    ctx = make_ctx(semaphore=FakeSemaphore({"status": "success", "template": {"id": 7, "name": "deploy"}}))
    assert verify("semaphore-task", {"task_id": "t-1", "template": "deploy"}, ctx).ok is True


def test_semaphore_not_success():
    ctx = make_ctx(semaphore=FakeSemaphore({"status": "failed"}))
    res = verify("semaphore-task", {"task_id": "t-1"}, ctx)
    assert res.ok is False
    assert "failed" in res.detail.lower() or "status" in res.detail.lower()


def test_semaphore_wrong_template():
    ctx = make_ctx(semaphore=FakeSemaphore({"status": "success", "template": {"id": 7, "name": "x"}}))
    res = verify("semaphore-task", {"task_id": "t-1", "template": "other"}, ctx)
    assert res.ok is False
    assert "template" in res.detail.lower()


def test_semaphore_fetch_error_is_sanitised():
    ctx = make_ctx(semaphore=FakeSemaphore(error=RuntimeError("SUPERSECRET")))
    res = verify("semaphore-task", {"task_id": "t-1"}, ctx)
    assert res.ok is False
    assert "SUPERSECRET" not in res.detail


def test_semaphore_missing_task_id():
    res = verify("semaphore-task", {}, make_ctx())
    assert res.ok is False
    assert "task_id" in res.detail


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #

def test_decision_pass():
    assert verify("decision", {"id": "D-209"}, make_ctx()).ok is True


def test_decision_missing():
    ctx = make_ctx(decisions=FakeDecisions(present={"D-100"}))
    res = verify("decision", {"id": "D-209"}, ctx)
    assert res.ok is False
    assert "D-209" in res.detail


def test_decision_missing_id_ref():
    res = verify("decision", {}, make_ctx())
    assert res.ok is False
    assert "id" in res.detail


def test_decision_adapter_error_fails():
    ctx = make_ctx(decisions=FakeDecisions(error=OSError("boom")))
    assert verify("decision", {"id": "D-209"}, ctx).ok is False


# --------------------------------------------------------------------------- #
# default_context
# --------------------------------------------------------------------------- #

def test_default_context_builds_real_adapters_without_network():
    from plangraph.keeper import evidence_ci, evidence_decisions, evidence_git, evidence_runs, evidence_semaphore

    config = {
        "repos": {"routing": "/repos/routing"},
        "ci": {"routing": "owner/repo"},
        "runs_dir": "/runs",
        "decisions_file": "/docs/DECISIONS.md",
    }
    ctx = ev.default_context(config, now=123.0)
    assert isinstance(ctx.git, evidence_git.GitAdapter)
    assert isinstance(ctx.ci, evidence_ci.CIAdapter)
    assert isinstance(ctx.runs, evidence_runs.RunRecordAdapter)
    assert isinstance(ctx.semaphore, evidence_semaphore.SemaphoreAdapter)
    assert isinstance(ctx.decisions, evidence_decisions.DecisionsAdapter)
    assert ctx.now == 123.0
    assert ctx.git.repo_path("routing") == "/repos/routing"
    assert ctx.ci.mode("routing") == "owner/repo"


def test_default_context_accepts_object_config():
    config = SimpleNamespace(repos={}, ci={}, runs_dir=None, decisions_file=None)
    ctx = ev.default_context(config, now=1.0)
    assert ctx.git.repo_path("anything") is None
