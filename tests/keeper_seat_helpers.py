"""Shared seat-coverage fixtures/helpers (not collected by pytest).

The keeper seat/family tests (``test_keeper_seat_coverage``,
``test_keeper_writer_regressions``) need ONE definition of the review world,
its commit shas and the claim-to-review seeders: re-exporting them from a
sibling ``test_*`` module breaks collection for every test in the importing
file the moment one name disappears, and a fixture object shared that way
diverges silently from a ``conftest``-level one.  They live here, and both
test modules import them from here.
"""
from __future__ import annotations

import pytest

from evidence_fakes import FakeGit, make_ctx
from keeper_util import World
from plangraph.keeper import auth

SHA_A = "a" * 40
SHA_B = "b" * 40
MERGE_SHA = "e" * 40
PHASE_SHA = "f" * 40
DECISION = "D-2217"

# name, role, family -- real family names, all on project demo.
EXTRA_PRINCIPALS = [
    ("claimant", "l1", "meta"),
    ("workerMeta", "worker", "meta"),
    ("rAnthropic1", "reviewer", "anthropic"),
    ("rAnthropic2", "reviewer", "anthropic"),
    ("rAlibaba", "reviewer", "alibaba"),
    ("rXiaomi", "reviewer", "xiaomi"),
    ("rDeepseek", "reviewer", "deepseek"),
    ("rGoogle", "reviewer", "google"),
]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    for name, role, family in EXTRA_PRINCIPALS:
        token = auth.add_principal(world.conn, name, role, ["demo"],
                                   family=family)
        world.tokens[name] = token
        world.principals[name] = auth.authenticate(world.conn, token)
    yield world
    world.close()


def _cov():
    """The module under test; imported here so the file collects while red."""
    from plangraph.keeper import coverage
    return coverage


def writer(value):
    """A ``Writer:`` trailer value (``<model-id> <run id>``)."""
    return {"Writer": value}


def git_with(trailers_by_sha):
    return FakeGit(trailers_by_sha=trailers_by_sha)


class RaisingGit(FakeGit):
    """``git show`` fails for every commit: the check must fail closed."""

    def trailers(self, repo, sha):
        raise RuntimeError("git show failed")


def to_review_with_commits(w, key, trailers_by_sha, *, git=None, who="claimant"):
    """Claim, attach one commit+ci row per sha, and submit."""
    w.store.ctx = make_ctx(git=git or git_with(trailers_by_sha))
    epoch = w.store.claim(w.p(who), key, lease_s=7200)["epoch"]
    for sha in trailers_by_sha:
        out = w.store.add_evidence(w.p(who), key, epoch, "commit+ci",
                                   {"commit": sha})
        assert out["verified"] is True
    w.store.submit(w.p(who), key, epoch)
    assert w.store._item(key)["state"] == "in_review"
    return epoch


def verdict_events(w, key):
    return w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type='review_verdict'"
        " ORDER BY seq", (key,)).fetchall()


def merge_git(branch_trailers, sha=MERGE_SHA, merge_trailers=None):
    """A ``FakeGit`` whose evidence *sha* is a merge commit.

    The merge commit itself carries only ``Plan-Item`` (that is what L1
    attaches); ``writer_trailers`` yields one dict per branch commit -- the
    flattened list the adapter returns for a merge, or for a PHASE merge whose
    parent2 range holds inner PR merges (D-2308).
    """
    return FakeGit(
        trailers_by_sha={sha: merge_trailers or {"Plan-Item": "demo:MERGE"}},
        branch_trailers_by_sha={sha: [dict(d) for d in branch_trailers]},
    )


def to_review_merge(w, key, git, sha=MERGE_SHA):
    """Claim as ``claimant``, attach the merge sha as commit+ci, submit."""
    return to_review_with_commits(
        w, key, {sha: git.trailers_by_sha.get(sha, {})}, git=git)
