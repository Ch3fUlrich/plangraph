"""``writer_trailers(ref=...)`` and the tag-object refusal, over REAL git repos.

KEEPER-EVIDENCE-MERGE-HEAD fix round (PR #309 review findings): a lane PR head
that merged the target ref INTO the lane is a 2-parent merge whose
``merge-base(parent1, parent2)..parent2`` range is the MAIN side, so the old
call shape attributed the change to main-side writers.  With ``ref`` given the
writers must come from the PR merge commit's range (the lane commits only);
``ref=None`` keeps the historical behaviour, documented here.  An annotated
tag OBJECT sha must never stand for a commit: its tag message carries
``Plan-Item``/``Writer`` trailers that name a writer who wrote no commit.

Hermetic: everything lives in ``tmp_path`` via ``git_helpers.git_cli`` (the
host's git config and ambient ``GIT_*`` are scrubbed); nothing is pushed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from git_helpers import git_cli, git_supports_init_branch
from keeper_seat_helpers import w  # noqa: F401 - the review world fixture
from plangraph.keeper.evidence_git import GitAdapter

#: `git init -b main` needs git >= 2.28; presence of a git binary is not enough.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not git_supports_init_branch(),
    reason="git >= 2.28 not available (git init -b)",
)

LANE_WRITERS = ["claude-opus-4 run-a", "gemini-2.5-pro run-b"]


def _commit(git, repo: Path, subject: str, trailers: dict[str, str]) -> str:
    """One ``--allow-empty`` commit with *trailers* in its message; returns its sha."""
    body = "".join(f"{key}: {value}\n" for key, value in trailers.items())
    git(repo, "commit", "--allow-empty", "-q", "-m", f"{subject}\n\n{body}")
    return git(repo, "rev-parse", "HEAD")


def _merge(git, repo: Path, branch: str, subject: str, plan_item: str) -> str:
    """A ``--no-ff`` merge carrying only ``Plan-Item``; returns its sha."""
    git(repo, "merge", "--no-ff", "-q", "-m",
        f"{subject}\n\nPlan-Item: {plan_item}", branch)
    return git(repo, "rev-parse", "HEAD")


def _lane_world(git, repo: Path) -> dict[str, str]:
    """c0 <- main(m1, m2, M=PR merge of f); lane f: x1, x2, H="merge main into f".

    ``H`` is the lane merge-head: 2 parents (x2 lane-side, m1 main-side) and
    NOT on main's first-parent line; ``M`` is the PR merge commit whose ^2 is
    ``H``.  Returns the shas by name.
    """
    git(repo, "commit", "--allow-empty", "-q", "-m", "c0")
    c0 = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "-c", "f")
    x1 = _commit(git, repo, "x1f",
                 {"Plan-Item": "demo:A", "Writer": "claude-opus-4 run-a"})
    x2 = _commit(git, repo, "x2f",
                 {"Plan-Item": "demo:A", "Writer": "gemini-2.5-pro run-b"})
    git(repo, "switch", "-q", "main")
    m1 = _commit(git, repo, "m1",
                 {"Plan-Item": "demo:M1", "Writer": "mimo-v2 run-m"})
    git(repo, "switch", "-q", "f")
    h = _merge(git, repo, "main", "merge main into f", "demo:A")
    git(repo, "switch", "-q", "main")
    m2 = _commit(git, repo, "m2",
                 {"Plan-Item": "demo:M2", "Writer": "qwen-max run-q"})
    git(repo, "config", "user.email", "merger@example.invalid")
    m = _merge(git, repo, "f", "Merge f", "demo:A")
    git(repo, "config", "user.email", "test@example.invalid")
    return {"c0": c0, "x1": x1, "x2": x2, "h": h, "m1": m1, "m2": m2, "m": m}


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    """``(repo, git, shas)`` for the lane merge-head history above."""
    repo = tmp_path / "r"
    git, git_init = git_cli(tmp_path, monkeypatch)
    git_init(repo, "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    shas = _lane_world(git, repo)
    return repo, git, shas


@pytest.fixture
def adapter() -> GitAdapter:
    return GitAdapter()


# --------------------------------------------------------------------- #
# lane head H = "merge main into the lane": writers are the LANE commits
# --------------------------------------------------------------------- #

def test_lane_head_with_ref_names_lane_writers_only(world, adapter):
    repo, _git, shas = world
    got = adapter.writer_trailers(str(repo), shas["h"], "main")
    # The lane commits (anthropic + google); never the main-side m1 (xiaomi).
    assert sorted(d.get("Writer") for d in got) == LANE_WRITERS
    assert {d.get("Plan-Item") for d in got} == {"demo:A"}


def test_lane_head_without_ref_keeps_old_behaviour(world, adapter):
    repo, _git, shas = world
    got = adapter.writer_trailers(str(repo), shas["h"])
    # Documented pre-fix behaviour with ref=None: writers come from
    # merge-base(parent1, parent2)..parent2 -- the MAIN side for a lane head.
    assert [d.get("Writer") for d in got] == ["mimo-v2 run-m"]


def test_leaf_and_pr_merge_with_ref_are_unchanged(world, adapter):
    repo, _git, shas = world
    # A non-merge commit stands for itself.
    assert adapter.writer_trailers(str(repo), shas["x1"], "main") == [
        {"Plan-Item": "demo:A", "Writer": "claude-opus-4 run-a"}]
    # The PR merge commit keeps naming the non-merge commits of its ^2 side.
    got = adapter.writer_trailers(str(repo), shas["m"], "main")
    assert sorted(d.get("Writer") for d in got) == LANE_WRITERS


def test_two_parent_merge_not_merged_via_pr_fails_closed(world, adapter):
    repo, git, shas = world
    git(repo, "switch", "-q", "-c", "u", shas["c0"])
    _commit(git, repo, "u1", {"Plan-Item": "demo:U", "Writer": "mimo-v2 run-u"})
    git(repo, "merge", "--no-ff", "-q", "-m",
        "Merge m1 into u\n\nPlan-Item: demo:U", shas["m1"])
    unmerged = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    # No PR merge introduced it: the adapter cannot prove its writers -> [].
    assert adapter.writer_trailers(str(repo), unmerged, "main") == []


def test_octopus_with_ref_fails_closed(world, adapter):
    repo, git, shas = world
    git(repo, "switch", "-q", "-c", "o1", shas["c0"])
    _commit(git, repo, "w1", {"Plan-Item": "demo:O", "Writer": "mimo-v2 run-1"})
    git(repo, "switch", "-q", "-c", "o2", shas["c0"])
    _commit(git, repo, "w2", {"Plan-Item": "demo:O", "Writer": "deepseek-r1 run-2"})
    git(repo, "switch", "-q", "-c", "o", shas["c0"])
    git(repo, "merge", "--no-ff", "-q", "-m",
        "Octopus\n\nPlan-Item: demo:O", "o1", "o2")
    octopus = git(repo, "rev-parse", "HEAD")
    assert len(git(repo, "rev-list", "--parents", "-n", "1", octopus).split()) == 4
    assert adapter.writer_trailers(str(repo), octopus, "main") == []


# --------------------------------------------------------------------- #
# an annotated tag OBJECT sha is not a commit (tag message != trailers)
# --------------------------------------------------------------------- #

def test_annotated_tag_object_is_refused(tmp_path: Path, monkeypatch, adapter):
    repo = tmp_path / "r"
    git, git_init = git_cli(tmp_path, monkeypatch)
    git_init(repo, "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "commit", "--allow-empty", "-q", "-m", "plain, no trailers")
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "-a", "v1", "-m",
        "Plan-Item: demo:A\nWriter: mimo-v2 run-fake")
    tag = git(repo, "rev-parse", "v1")  # the tag OBJECT sha, not peeled
    assert tag != commit

    # The tag message's trailers must never bind: the object is not a commit.
    assert adapter.commit_exists(str(repo), tag) is False
    assert adapter.trailers(str(repo), tag) == {}
    assert adapter.writer_trailers(str(repo), tag, "main") == []


def test_lightweight_tag_and_plain_sha_are_accepted(tmp_path: Path, monkeypatch,
                                                    adapter):
    repo = tmp_path / "r"
    git, git_init = git_cli(tmp_path, monkeypatch)
    git_init(repo, "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    commit = _commit(git, repo, "one",
                     {"Plan-Item": "demo:A", "Writer": "mimo-v2 run1"})
    git(repo, "tag", "v2")  # lightweight: points straight at the commit
    sha = git(repo, "rev-parse", "v2")
    assert sha == commit

    assert adapter.commit_exists(str(repo), sha) is True
    assert adapter.trailers(str(repo), sha) == {
        "Plan-Item": "demo:A", "Writer": "mimo-v2 run1"}


# --------------------------------------------------------------------- #
# verifier level: the keeper's writer-family check over the real-repo case
# --------------------------------------------------------------------- #

def test_coverage_families_for_lane_head_are_lane_side(w, tmp_path: Path,
                                                       monkeypatch):
    from evidence_fakes import FakeCI, make_ctx
    from keeper_util import seed
    from plangraph.keeper import evidence as ev
    from plangraph.keeper.coverage import writer_families

    repo = tmp_path / "r"
    git, git_init = git_cli(tmp_path, monkeypatch)
    git_init(repo, "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")

    w.store.ctx = make_ctx(
        git=GitAdapter({"demo": {"path": str(repo), "ref": "main"}}))
    # the REAL commit+ci verifier: merge_commit_of must accept the head
    w.store.verifiers["commit+ci"] = ev.VERIFIERS["commit+ci"]
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    epoch = w.store.claim(w.p("claimant"), key, lease_s=7200)["epoch"]
    # the history is built AFTER the claim: a claim refuses an item whose
    # Plan-Item commit is already on the default branch
    shas = _lane_world(git, repo)
    h = shas["h"]
    w.store.ctx.ci = FakeCI(
        mode="owner/repo",
        runs=[{"name": "build", "head_sha": h, "conclusion": "success"}])
    out = w.store.add_evidence(w.p("claimant"), key, epoch,
                               "commit+ci", {"commit": h})
    assert out["verified"] is True, out
    w.store.submit(w.p("claimant"), key, epoch)

    families = writer_families(w.store, w.store._item(key), w.store.ctx)
    # The lane's writers (anthropic + google), never the main side's.
    assert sorted(families) == ["anthropic", "google"]
    assert "xiaomi" not in families
