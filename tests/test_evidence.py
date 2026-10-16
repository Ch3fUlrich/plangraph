"""Tests for the keeper evidence registry and the ``commit+ci`` verifier.

Fakes live in ``evidence_fakes``; every pass and every documented failure path
is exercised without touching git, the network or the filesystem. Real adapters
are covered in ``test_evidence_adapters.py``; the other three verifiers in
``test_evidence_verifiers.py``.
"""

from __future__ import annotations

import pytest

from evidence_fakes import CLAIM, ITEM, OTHER_SHA, SHA, FakeCI, FakeGit, make_ctx, verify
from plangraph.keeper import evidence as ev
from plangraph.keeper.evidence_types import Result, as_epoch


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


def test_commit_ci_commit_older_than_claim():
    # 1000 s before the claim is far outside the default 120 s skew.
    ctx = make_ctx(git=FakeGit(commit_time=CLAIM - 1000))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "claim" in res.detail.lower() or "predate" in res.detail.lower()


def test_commit_ci_no_claim_time_fails_closed():
    ctx = make_ctx()
    ctx.claim_time = None
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "claim" in res.detail.lower()


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
