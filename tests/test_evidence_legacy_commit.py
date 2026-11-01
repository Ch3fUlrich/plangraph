"""X7b: the legacy evidence kind ``commit``.

A bare commit is the *legacy* proof of work: it passes on existence plus
``main`` ancestry and nothing else — no ``Plan-Item`` trailer, no date window,
no CI.  Because those checks are exactly what makes ``commit+ci`` trustworthy,
kind ``commit`` is a router-only, legacy-only attachment.
"""
import subprocess
from types import SimpleNamespace

import pytest
from git_identity import identity_env

from keeper_util import World, seed, to_claimed
from plangraph.keeper import evidence
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.evidence_types import Context

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available")

KEY = "demo:A"


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True, env=env or identity_env()).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real repo: one commit on ``main``, one on a side branch only."""
    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "f.txt").write_text("x", encoding="utf-8")
    _git(r, "add", "f.txt")
    # Deliberately trailer-less: kind `commit` must not care.
    _git(r, "commit", "-q", "-m", "legacy work")
    _git(r, "branch", "-M", "main")
    ancestor = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "-b", "side")
    (r / "g.txt").write_text("y", encoding="utf-8")
    _git(r, "add", "g.txt")
    _git(r, "commit", "-q", "-m", "off main")
    side = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    return SimpleNamespace(path=r, ancestor=ancestor, side=side)


def _ctx(repo):
    return Context(git=GitAdapter({"demo": str(repo.path)}))


def _item(project="demo"):
    return {"key": f"{project}:A", "project": project, "epoch": 1}


# --------------------------------------------------------------- verifier
def test_commit_verifier_registered():
    assert "commit" in evidence.VERIFIERS
    assert evidence.VERIFIERS["commit"].kind == "commit"


def test_ancestor_of_main_passes(repo):
    result = evidence.verify("commit", _item(), {"commit": repo.ancestor}, _ctx(repo))
    assert result.ok is True


def test_non_ancestor_refused(repo):
    result = evidence.verify("commit", _item(), {"commit": repo.side}, _ctx(repo))
    assert result.ok is False
    assert "main" in result.detail


def test_unknown_sha_refused(repo):
    result = evidence.verify("commit", _item(),
                             {"commit": "0" * 40}, _ctx(repo))
    assert result.ok is False


def test_unregistered_project_refused(repo):
    result = evidence.verify("commit", _item("other"),
                             {"commit": repo.ancestor}, _ctx(repo))
    assert result.ok is False
    assert "other" in result.detail


def test_no_sha_in_ref_refused(repo):
    result = evidence.verify("commit", _item(), {}, _ctx(repo))
    assert result.ok is False


def test_missing_repo_dir_fails_closed(tmp_path):
    ctx = Context(git=GitAdapter({"demo": str(tmp_path / "nope")}))
    result = evidence.verify("commit", _item(), {"commit": "a" * 40}, ctx)
    assert result.ok is False


# ---------------------------------------------------------------- store
@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.store.verifiers["commit"] = evidence.VERIFIERS["commit"]
    seed(w, [("A", [])], evidence_kinds=("commit",))
    to_claimed(w, KEY)
    yield w
    w.close()


def _with_git(world, repo):
    world.store.ctx = SimpleNamespace(git=GitAdapter({"demo": str(repo.path)}))


def test_router_attaches_verified_commit(world, repo):
    _with_git(world, repo)
    out = world.store.add_evidence_router(world.p("router"), KEY, "commit",
                                          {"commit": repo.ancestor})
    assert out["verified"] is True
    row = world.conn.execute("SELECT kind,verified FROM evidence WHERE key=?",
                             (KEY,)).fetchone()
    assert (row["kind"], bool(row["verified"])) == ("commit", 1)


def test_router_attaches_non_ancestor_unverified(world, repo):
    _with_git(world, repo)
    out = world.store.add_evidence_router(world.p("router"), KEY, "commit",
                                          {"commit": repo.side})
    assert out["verified"] is False


def test_claimant_may_not_attach_commit(world, repo):
    _with_git(world, repo)
    epoch = world.conn.execute("SELECT epoch FROM items WHERE key=?",
                               (KEY,)).fetchone()["epoch"]
    with pytest.raises(Refused) as exc:
        world.store.add_evidence(world.p("l1main"), KEY, epoch, "commit",
                                 {"commit": repo.ancestor})
    assert exc.value.code == "forbidden"
    assert world.conn.execute("SELECT COUNT(*) FROM evidence WHERE key=?",
                              (KEY,)).fetchone()[0] == 0
