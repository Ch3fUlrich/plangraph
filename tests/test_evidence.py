"""Tests for the keeper evidence registry and the ``commit+ci`` verifier.

Fakes live in ``evidence_fakes``; every pass and every documented failure path
is exercised without touching git, the network or the filesystem. Real adapters
are covered in ``test_evidence_adapters.py``; the other three verifiers in
``test_evidence_verifiers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_fakes import (
    CLAIM, ITEM, MERGE_EMAIL, OTHER_SHA, SHA, FakeCI, FakeDecisions, FakeGit,
    FakeRuns, FakeSemaphore, make_ctx, verify,
)
from plangraph.keeper import evidence as ev
from plangraph.keeper.evidence_types import Context, Result, as_epoch


# --------------------------------------------------------------------------- #
# registry / types
# --------------------------------------------------------------------------- #

def test_registry_has_exactly_the_six_kinds():
    # X7b adds the legacy router-only `commit` kind; MK-FIXES-ERROR the `error` record.
    assert set(ev.VERIFIERS) == {"commit+ci", "commit", "run-record",
                                 "semaphore-task", "decision", "error"}


def test_unknown_kind_fails_validation():
    res = ev.verify("teleport", ITEM, {}, make_ctx())
    assert res.ok is False
    assert "teleport" in res.detail


def test_result_is_a_dataclass_with_ok_detail_ref():
    res = Result(True, "fine", {"x": 1})
    assert res.ok is True and res.detail == "fine" and res.ref == {"x": 1}


@pytest.mark.parametrize("value,expected", [
    (None, None),
    (100, 100.0),
    (100.5, 100.5),
    ("1700000000", 1_700_000_000.0),
    ("2023-11-14T22:13:20Z", 1_700_000_000.0),
    ("not-a-time", None),
])
def test_as_epoch(value, expected):
    got = as_epoch(value)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# commit+ci: pass
# --------------------------------------------------------------------------- #

def test_commit_ci_pass():
    ctx = make_ctx()
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is True, res.detail
    assert res.ref == {"commit": SHA}
    assert ctx.ci.calls == [("owner/repo", SHA)]


def test_commit_ci_pass_with_bare_id_trailer():
    ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": ITEM["id"]}))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is True


def test_commit_ci_pass_ci_none_needs_no_ci_lookup():
    ctx = make_ctx(ci=FakeCI(mode="none", runs=[]))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is True
    assert ctx.ci.calls == []


def test_commit_ci_commit_exactly_at_claim_passes():
    ctx = make_ctx(git=FakeGit(commit_time=CLAIM))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is True


# --------------------------------------------------------------------------- #
# commit+ci: failure paths
# --------------------------------------------------------------------------- #

def test_commit_ci_missing_commit_ref():
    res = verify("commit+ci", {}, make_ctx())
    assert res.ok is False
    assert "commit" in res.detail


def test_commit_ci_missing_repo_config():
    ctx = make_ctx(git=FakeGit(repo=None))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "routing" in res.detail


def test_commit_ci_unknown_sha():
    ctx = make_ctx(git=FakeGit(exists=False))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "exist" in res.detail.lower()


def test_commit_ci_off_main_sha():
    ctx = make_ctx(git=FakeGit(ancestor=False))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "ancestor" in res.detail.lower() or "main" in res.detail.lower()


def test_commit_ci_missing_trailer():
    ctx = make_ctx(git=FakeGit(trailers={}))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "Plan-Item" in res.detail


def test_commit_ci_wrong_trailer_value():
    ctx = make_ctx(git=FakeGit(trailers={"Plan-Item": "routing:SOMETHING-ELSE"}))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is False


# --------------------------------------------------------------------------- #
# commit+ci: F1 trailer binding replaces the claim-time window
# --------------------------------------------------------------------------- #

def test_commit_ci_merged_pr_passes() -> None:
    ctx = make_ctx()
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is True, res.detail

    ctx_principal = make_ctx(merge_principals=(MERGE_EMAIL.upper(),))
    res_principal = verify("commit+ci", {"commit": SHA}, ctx_principal)
    assert res_principal.ok is True, res_principal.detail


def test_commit_ci_commit_without_merged_pr_refused() -> None:
    ctx = make_ctx(git=FakeGit(merge=None))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "merged PR" in res.detail


def test_commit_ci_unmerged_commit_not_ancestor_refused() -> None:
    ctx = make_ctx(git=FakeGit(ancestor=False))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "not an ancestor" in res.detail


def test_commit_ci_merge_principals_allowlist() -> None:
    ctx_disallowed = make_ctx(merge_principals=("other@example.invalid",))
    res_disallowed = verify("commit+ci", {"commit": SHA}, ctx_disallowed)
    assert res_disallowed.ok is False
    assert "merge principal" in res_disallowed.detail

    ctx_allowed = make_ctx(merge_principals=())
    res_allowed = verify("commit+ci", {"commit": SHA}, ctx_allowed)
    assert res_allowed.ok is True, res_allowed.detail


def test_commit_ci_git_adapter_lacking_merge_commit_of_fails_closed() -> None:
    class _NoMergeGit:
        def __init__(self, git: FakeGit) -> None:
            self._git = git

        def __getattr__(self, name: str):
            if name == "merge_commit_of":
                raise AttributeError(name)
            return getattr(self._git, name)

    ctx = make_ctx(git=_NoMergeGit(FakeGit()))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "cannot prove a merged PR" in res.detail


def test_commit_ci_merge_commit_of_exception_fails_closed() -> None:
    ctx = make_ctx(git=FakeGit(merge_error=RuntimeError("boom")))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "RuntimeError" in res.detail
    assert "boom" not in res.detail


def test_commit_ci_ancestry_floor() -> None:
    ctx_pass = make_ctx(
        bind_floor_required=True,
        bind_floor_sha=OTHER_SHA,
        git=FakeGit(descends=True),
    )
    res_pass = verify("commit+ci", {"commit": SHA}, ctx_pass)
    assert res_pass.ok is True, res_pass.detail

    ctx_no_descend = make_ctx(
        bind_floor_required=True,
        bind_floor_sha=OTHER_SHA,
        git=FakeGit(descends=False),
    )
    res_no_descend = verify("commit+ci", {"commit": SHA}, ctx_no_descend)
    assert res_no_descend.ok is False
    assert "does not descend" in res_no_descend.detail

    ctx_no_floor = make_ctx(bind_floor_required=True, bind_floor_sha=None)
    res_no_floor = verify("commit+ci", {"commit": SHA}, ctx_no_floor)
    assert res_no_floor.ok is False
    assert "no ancestry floor" in res_no_floor.detail

    ctx_floor_err = make_ctx(
        bind_floor_required=True,
        bind_floor_sha=OTHER_SHA,
        git=FakeGit(floor_error=RuntimeError("boom")),
    )
    res_floor_err = verify("commit+ci", {"commit": SHA}, ctx_floor_err)
    assert res_floor_err.ok is False
    assert "ancestry floor" in res_floor_err.detail

    ctx_first_claim = make_ctx(bind_floor_required=False, bind_floor_sha=None)
    res_first_claim = verify("commit+ci", {"commit": SHA}, ctx_first_claim)
    assert res_first_claim.ok is True, res_first_claim.detail


def test_commit_ci_context_default_requires_floor() -> None:
    assert Context().bind_floor_required is True

    real_ctx = Context(
        git=FakeGit(),
        ci=FakeCI(),
        runs=FakeRuns(),
        semaphore=FakeSemaphore(),
        decisions=FakeDecisions(),
        now=CLAIM + 50,
    )
    res_real = verify("commit+ci", {"commit": SHA}, real_ctx)
    assert res_real.ok is False
    assert "no ancestry floor" in res_real.detail

    ns_ctx = make_ctx()
    delattr(ns_ctx, "bind_floor_required")
    res_ns = verify("commit+ci", {"commit": SHA}, ns_ctx)
    assert res_ns.ok is False
    assert "no ancestry floor" in res_ns.detail


def test_commit_ci_different_plan_item_key_refused_even_when_floor_and_merge_pass() -> None:
    ctx = make_ctx(
        bind_floor_required=True,
        bind_floor_sha=OTHER_SHA,
        git=FakeGit(
            trailers={"Plan-Item": "routing:T9-OTHER"},
            descends=True,
        ),
    )
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "Plan-Item" in res.detail


def test_default_context_merge_principals() -> None:
    assert ev.default_context(
        {"merge_principals": ["A@x.invalid", " b@x.invalid "]}
    ).merge_principals == ("A@x.invalid", "b@x.invalid")

    assert ev.default_context(
        {"merge_principals": "A@x.invalid, b@x.invalid"}
    ).merge_principals == ("A@x.invalid", "b@x.invalid")

    assert ev.default_context({}).merge_principals == ()


def test_commit_ci_red_ci():
    ctx = make_ctx(ci=FakeCI(runs=[{"name": "build", "head_sha": SHA, "conclusion": "failure"}]))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "green" in res.detail.lower() or "ci" in res.detail.lower()


def test_commit_ci_pending_ci():
    ctx = make_ctx(ci=FakeCI(runs=[{"name": "build", "head_sha": SHA, "conclusion": ""}]))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is False


def test_commit_ci_for_another_sha():
    ctx = make_ctx(ci=FakeCI(runs=[{"name": "build", "head_sha": OTHER_SHA, "conclusion": "success"}]))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "sha" in res.detail.lower() or "another" in res.detail.lower()


def test_commit_ci_no_runs_fails():
    ctx = make_ctx(ci=FakeCI(runs=[]))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is False


def test_commit_ci_unconfigured_ci_fails():
    ctx = make_ctx(ci=FakeCI(mode=None, runs=[]))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "ci" in res.detail.lower()


def test_commit_ci_lookup_error_is_sanitised():
    ctx = make_ctx(ci=FakeCI(error=RuntimeError("SECRET-TOKEN-abc")))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "SECRET-TOKEN" not in res.detail


# --------------------------------------------------------------------------- #
# commit+ci: newest run per check name
# --------------------------------------------------------------------------- #

def test_commit_ci_newest_per_name_old_failure_newer_success_ok():
    runs = [
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "failure"},
        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True


def test_commit_ci_newest_per_name_old_success_newer_failure_fails():
    runs = [
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "failure"},
    ]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "build=failure" in res.detail


def test_commit_ci_newest_tie_break_started_at():
    runs = [
        {"name": "build", "id": 5, "started_at": "2026-01-01T00:00:01Z",
         "head_sha": SHA, "conclusion": "failure"},
        {"name": "build", "id": 5, "started_at": "2026-01-01T00:00:02Z",
         "head_sha": SHA, "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True


def test_commit_ci_newest_numeric_string_id_ranks_numerically():
    runs = [
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
        {"name": "build", "id": "99", "head_sha": SHA, "conclusion": "failure"},
    ]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "build=failure" in res.detail


def test_commit_ci_newest_non_int_id_counts_as_zero():
    runs = [
        {"name": "build", "id": "abc", "started_at": "", "head_sha": SHA,
         "conclusion": "failure"},
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
    tie_runs = [
        {"name": "build", "id": "abc", "started_at": "2026-01-01T00:00:01Z",
         "head_sha": SHA, "conclusion": "failure"},
        {"name": "build", "id": None, "started_at": "2026-01-01T00:00:02Z",
         "head_sha": SHA, "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=tie_runs))).ok is True


def test_commit_ci_stale_success_with_in_progress_status_is_pending():
    runs = [{"name": "build", "id": 1, "head_sha": SHA, "status": "in_progress",
             "conclusion": "success"}]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "pending" in res.detail.lower()


def test_commit_ci_missing_name_groups_under_unknown():
    runs = [{"id": 1, "head_sha": SHA, "conclusion": "failure"}]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "?=failure" in res.detail


def test_commit_ci_completed_none_conclusion_is_pending():
    runs = [{"name": "build", "id": 1, "head_sha": SHA, "status": "completed",
             "conclusion": None}]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "pending" in res.detail.lower()


def test_commit_ci_conclusion_whitespace_and_case_success_ok():
    runs = [{"name": "build", "id": 1, "head_sha": SHA, "conclusion": " SUCCESS "}]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True


def test_commit_ci_conclusion_mixed_case_failure_fails():
    runs = [{"name": "build", "id": 1, "head_sha": SHA, "conclusion": "Failure"}]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "Failure" in res.detail


def test_commit_ci_unknown_conclusion_weird_fails_naming_it():
    runs = [{"name": "lint", "id": 1, "head_sha": SHA, "conclusion": "weird"}]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "weird" in res.detail
    assert "lint" in res.detail


@pytest.mark.parametrize("conclusion", [
    "failure", "cancelled", "timed_out", "action_required", "stale", "bogus",
])
def test_commit_ci_each_fail_conclusion_names_the_check(conclusion):
    runs = [{"name": "lint", "id": 1, "head_sha": SHA, "conclusion": conclusion}]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "lint" in res.detail
    assert conclusion in res.detail
    assert "green" in res.detail.lower()


def test_commit_ci_skipped_plus_success_ok():
    runs = [
        {"name": "win", "id": 1, "head_sha": SHA, "conclusion": "skipped"},
        {"name": "linux", "id": 2, "head_sha": SHA, "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True


def test_commit_ci_neutral_ok():
    runs = [{"name": "lint", "id": 1, "head_sha": SHA, "conclusion": "neutral"}]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True


def test_commit_ci_all_skipped_fails_closed():
    runs = [
        {"name": "a", "id": 1, "head_sha": SHA, "conclusion": "skipped"},
        {"name": "b", "id": 2, "head_sha": SHA, "conclusion": "skipped"},
    ]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "only skipped" in res.detail.lower()


def test_commit_ci_pending_in_progress_fails_with_pending():
    runs = [{"name": "build", "id": 1, "head_sha": SHA, "status": "in_progress",
             "conclusion": None}]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "pending" in res.detail.lower()


def test_commit_ci_a1b1da808_pending_then_ok():
    pending_runs = [
        {"name": "Windows suite", "id": 1, "head_sha": SHA,
         "status": "completed", "conclusion": "skipped"},
        {"name": "Windows suite", "id": 2, "head_sha": SHA,
         "status": "completed", "conclusion": "skipped"},
        {"name": "shard a", "id": 3, "head_sha": SHA,
         "status": "completed", "conclusion": "success"},
        {"name": "shard b", "id": 10, "head_sha": SHA,
         "status": "completed", "conclusion": "success"},
        {"name": "shard b", "id": 11, "head_sha": SHA,
         "status": "in_progress", "conclusion": None},
    ]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=pending_runs)))
    assert res.ok is False
    assert "pending" in res.detail.lower()
    ok_runs = pending_runs + [
        {"name": "shard b", "id": 12, "head_sha": SHA,
         "status": "completed", "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=ok_runs))).ok is True


def test_commit_ci_no_runs_fails_closed_newest_logic():
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=[])))
    assert res.ok is False


def test_commit_ci_same_name_different_suites_both_evaluated():
    runs = [
        {"name": "test", "id": 1, "head_sha": SHA,
         "check_suite": {"id": 100}, "status": "completed", "conclusion": "failure"},
        {"name": "test", "id": 2, "head_sha": SHA,
         "check_suite": {"id": 200}, "status": "completed", "conclusion": "success"},
    ]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
    assert res.ok is False
    assert "test" in res.detail


def test_commit_ci_same_suite_rerun_supersedes():
    runs = [
        {"name": "test", "id": 1, "head_sha": SHA,
         "check_suite": {"id": 100}, "status": "completed", "conclusion": "failure"},
        {"name": "test", "id": 2, "head_sha": SHA,
         "check_suite": {"id": 100}, "status": "completed", "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True


def test_commit_ci_suite_grouped_a1b1da808_pending_then_ok():
    pending_runs = [
        {"name": "Windows suite", "id": 1, "head_sha": SHA,
         "check_suite": {"id": 10}, "status": "completed", "conclusion": "skipped"},
        {"name": "Windows suite", "id": 2, "head_sha": SHA,
         "check_suite": {"id": 11}, "status": "completed", "conclusion": "skipped"},
        {"name": "shard a", "id": 3, "head_sha": SHA,
         "check_suite": {"id": 10}, "status": "completed", "conclusion": "success"},
        {"name": "shard b", "id": 10, "head_sha": SHA,
         "check_suite": {"id": 10}, "status": "completed", "conclusion": "success"},
        {"name": "shard b", "id": 11, "head_sha": SHA,
         "check_suite": {"id": 11}, "status": "in_progress", "conclusion": None},
    ]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=pending_runs)))
    assert res.ok is False
    assert "pending" in res.detail.lower()
    ok_runs = pending_runs + [
        {"name": "shard b", "id": 12, "head_sha": SHA,
         "check_suite": {"id": 11}, "status": "completed", "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=ok_runs))).ok is True


def test_commit_ci_legacy_runs_without_suite_group_by_name():
    runs = [
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "failure"},
        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "success"},
    ]
    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
    flipped = [
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "failure"},
    ]
    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=flipped)))
    assert res.ok is False
    assert "build=failure" in res.detail


def test_ci_conclusion_all_non_dict_fails_closed():
    assert ev._ci_conclusion([None, 42, "x"], SHA) == f"no CI check-runs found for {SHA}"


# --------------------------------------------------------------------------- #
# commit+ci: workflow-dispatch runs grouped across suites
# --------------------------------------------------------------------------- #

def _wf_run(run_id, *, sid, rid, conclusion, status="completed",
            name="pytest", event="workflow_dispatch", wf=7):
    """A real-shaped check-run with the adapter-added ``workflow`` field."""
    run = {"name": name, "id": rid, "head_sha": SHA, "status": status,
           "conclusion": conclusion, "check_suite": {"id": sid},
           "app": {"slug": "github-actions"},
           "started_at": "2026-10-16T09:00:00Z",
           "workflow": {"event": event, "workflow_id": wf, "name": "tests",
                        "run_id": run_id}}
    return run


def test_ci_wf_cancelled_then_green_dispatch_different_suites_ok():
    # One workflow dispatched twice: the first attempt was cancelled in suite A,
    # the rerun went green in suite B -- the wf group must pick the rerun.
    runs = [
        _wf_run(9001, sid=11, rid=1, conclusion="cancelled"),
        _wf_run(9002, sid=12, rid=2, conclusion="success"),
    ]
    assert ev._ci_conclusion(runs, SHA) is None


def test_ci_wf_green_then_failed_dispatch_fails():
    # The later dispatch failed even though its check-run id is LOWER than the
    # green run's: inside a wf group the workflow run id decides, not the id.
    runs = [
        _wf_run(9001, sid=11, rid=500, conclusion="success"),
        _wf_run(9002, sid=11, rid=400, conclusion="failure"),
    ]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None
    assert "not green" in detail
    assert "pytest=failure" in detail


def test_ci_wf_pr_only_two_suites_stay_separate():
    # No dispatch exists for this workflow, so PR runs keep the old
    # (suite, app, name) keys: the failed suite still blocks today's way.
    runs = [
        _wf_run(8001, sid=21, rid=1, conclusion="failure", event="pull_request"),
        _wf_run(8002, sid=22, rid=2, conclusion="success", event="pull_request"),
    ]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None
    assert "not green" in detail
    assert "failure" in detail
    assert "pending" not in detail.lower()


def test_ci_wf_pr_failure_then_later_green_dispatch_ok():
    # The PR run's suite failed, but a later dispatch of the same workflow went
    # green: the PR run joins the wf group and the green rerun wins.
    runs = [
        _wf_run(8001, sid=31, rid=10, conclusion="failure", event="pull_request"),
        _wf_run(8002, sid=32, rid=11, conclusion="success"),
    ]
    assert ev._ci_conclusion(runs, SHA) is None


def test_ci_wf_dispatch_failure_after_pr_green_fails():
    # Same workflow, reversed verdicts: the later dispatch failure blocks even
    # though the PR run (separate suite) was green.
    runs = [
        _wf_run(8001, sid=41, rid=30, conclusion="success", event="pull_request"),
        _wf_run(8002, sid=42, rid=25, conclusion="failure"),
    ]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None
    assert "not green" in detail
    assert "pytest=failure" in detail


def test_ci_wf_newer_in_progress_dispatch_after_green_is_pending():
    # A dispatch that is still running outranks the older green one, so the
    # group waits even though its check-run id is lower.
    runs = [
        _wf_run(9001, sid=51, rid=60, conclusion="success"),
        _wf_run(9002, sid=51, rid=55, conclusion=None, status="in_progress"),
    ]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None
    assert "pending" in detail.lower()


def test_ci_wf_check_run_id_not_chronological_green_wins():
    # Real data: a rerun gets check-run ids that can be LOWER than the older,
    # failed run's ids. Ranking is (workflow.run_id, check-run id, started_at).
    runs = [
        _wf_run(9000, sid=61, rid=900, conclusion="failure"),
        _wf_run(9001, sid=62, rid=7, conclusion="success"),
    ]
    assert ev._ci_conclusion(runs, SHA) is None


def test_ci_wf_group_without_completed_run_is_pending():
    runs = [_wf_run(9001, sid=71, rid=1, conclusion=None, status="in_progress")]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None
    assert "pending" in detail.lower()


def test_ci_wf_newer_skipped_dispatch_does_not_mask_older_failure():
    # Judge probe: workflow 7 dispatched twice, both completed, different
    # suites, github-actions app. Run 100 ran pytest (job failed) and lint; the
    # newer run 200 skipped pytest with a job-level `if:` and kept lint green.
    # The skipped run must not win the wf group: the older real verdict still
    # blocks, instead of the false green a newest-run-wins rule reported.
    runs = [
        _wf_run(100, sid=101, rid=11, conclusion="failure"),
        _wf_run(100, sid=101, rid=12, conclusion="success", name="lint"),
        _wf_run(200, sid=102, rid=21, conclusion="skipped"),
        _wf_run(200, sid=102, rid=22, conclusion="success", name="lint"),
    ]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None
    assert "pytest" in detail
    assert "failure" in detail
    assert "pending" not in detail.lower()


def test_ci_wf_group_all_completed_runs_skipped_is_only_skipped():
    # When every completed run of a wf group is skipped/neutral there is no
    # real verdict to prefer, so the newest of them wins and the standing
    # "CI only skipped" rule applies unchanged.
    runs = [
        _wf_run(100, sid=111, rid=11, conclusion="skipped"),
        _wf_run(200, sid=112, rid=21, conclusion="skipped"),
    ]
    assert ev._ci_conclusion(runs, SHA) == f"CI only skipped for {SHA}"


def test_ci_wf_newer_neutral_dispatch_keeps_older_success_ok():
    # A neutral verdict is not a real verdict either: the older success is the
    # newest non-skipped/non-neutral run and still makes the group green.
    runs = [
        _wf_run(100, sid=121, rid=11, conclusion="success"),
        _wf_run(200, sid=122, rid=21, conclusion="neutral"),
    ]
    assert ev._ci_conclusion(runs, SHA) is None
    # ...and symmetrically a newer neutral never masks an older failure.
    blocked = [
        _wf_run(100, sid=123, rid=13, conclusion="failure"),
        _wf_run(200, sid=124, rid=23, conclusion="neutral"),
    ]
    detail = ev._ci_conclusion(blocked, SHA)
    assert detail is not None
    assert "pytest=failure" in detail


def test_ci_wf_ambiguous_name_disambiguates_with_workflow_id():
    runs = [
        _wf_run(9001, sid=11, rid=1, conclusion="failure", wf=7),
        _wf_run(9002, sid=12, rid=2, conclusion="failure", wf=99),
    ]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None
    assert "pytest (workflow 7)=failure" in detail
    assert "pytest (workflow 99)=failure" in detail


def test_ci_wf_missing_workflow_id_stays_plain_suite_groups():
    # A workflow dict with no usable workflow_id must behave exactly like a
    # missing 'workflow' key: the suites stay separate plain groups, so the
    # older failure still blocks although a newer green run sits next to it
    # (a shared ("wf", 0, ...) group would let that green mask the failure).
    failed = _wf_run(9001, sid=81, rid=1, conclusion="failure")
    green = _wf_run(9002, sid=82, rid=2, conclusion="success", wf=None)
    failed["workflow"].pop("workflow_id")
    assert ev._ci_conclusion([failed, green], SHA) == f"CI is not green for {SHA} (pytest=failure)"
    # Reversed verdicts split the same way: the newer failure in its own suite
    # blocks on its own, never folded into the other suite's run.
    older_green = _wf_run(9001, sid=83, rid=3, conclusion="success", wf=None)
    newer_failed = _wf_run(9002, sid=84, rid=4, conclusion="failure", wf=None)
    assert ev._ci_conclusion([older_green, newer_failed], SHA) == f"CI is not green for {SHA} (pytest=failure)"


def test_ci_wf_bool_workflow_id_is_not_grouped():
    # bool is not an id (same coercion as check-run ids): workflow_id=True must
    # not forge a ("wf", 0, ...) group, so both suites stay plain groups and
    # both failures are named instead of one hiding behind the other.
    runs = [
        _wf_run(9001, sid=91, rid=1, conclusion="failure", wf=True),
        _wf_run(9002, sid=92, rid=2, conclusion="failure", wf=True),
    ]
    detail = ev._ci_conclusion(runs, SHA)
    assert detail == (f"CI is not green for {SHA} "
                      "(pytest (suite 91)=failure, pytest (suite 92)=failure)")


def test_ci_wf_missing_run_id_stays_plain_suite_groups():
    # A 'workflow' dict with no usable run_id counts as no 'workflow' at all,
    # exactly like a missing workflow_id: without a positive run_id neither
    # dispatch may join a wf group, so both keep their own plain
    # (suite, app, name) key and the newer failure blocks instead of losing the
    # rank race inside a shared ("wf", 7, ...) group, where the older green run
    # wins on its higher check-run id (900 vs 7) and wrongly reports green.
    older_green = _wf_run(9000, sid=95, rid=900, conclusion="success")
    newer_failed = _wf_run(9001, sid=96, rid=7, conclusion="failure")
    older_green["workflow"].pop("run_id")
    newer_failed["workflow"].pop("run_id")
    assert ev._ci_conclusion([older_green, newer_failed], SHA) == f"CI is not green for {SHA} (pytest=failure)"
    # The same trap with only the newer failure lacking a run_id: its run with
    # run_id 9000 still ranks the green run newest inside the merged group, so
    # the failure must again sit in its own plain suite group and block.
    older_green = _wf_run(9000, sid=97, rid=900, conclusion="success")
    newer_failed = _wf_run(9001, sid=98, rid=7, conclusion="failure")
    newer_failed["workflow"].pop("run_id")
    assert ev._ci_conclusion([older_green, newer_failed], SHA) == f"CI is not green for {SHA} (pytest=failure)"


def test_ci_conclusion_runs_without_workflow_key_unchanged():
    runs = [
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "failure"},
        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "success"},
    ]
    assert ev._ci_conclusion(runs, SHA) is None
    flipped = [
        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "failure"},
    ]
    detail = ev._ci_conclusion(flipped, SHA)
    assert detail is not None
    assert "build=failure" in detail


def test_ci_conclusion_replay_fixture_prererun_ok():
    # Replay of the real 998f8f3 pre-rerun payload: a cancelled first attempt,
    # a cancelled PR run and a dispatch whose pytest-postgres failed, all beaten
    # by the newest dispatch run -- verdict must be green.
    data = json.loads(
        (Path(__file__).parent / "fixtures" / "ci_998f8f3_prererun.json").read_text()
    )
    assert ev._ci_conclusion(data["check_runs"], data["sha"]) is None
