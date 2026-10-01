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

def test_registry_has_exactly_the_four_kinds():
    assert set(ev.VERIFIERS) == {"commit+ci", "run-record", "semaphore-task", "decision"}


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
    ctx = make_ctx(git=FakeGit(commit_time=CLAIM - 1))
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
