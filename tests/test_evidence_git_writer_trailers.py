"""``GitAdapter.writer_trailers`` against a REAL temporary git repository.

Items 15 and 16 of the SEAT-COVERAGE-KEEPER ADDENDUM: a merge (or PHASE merge)
evidence commit carries only ``Plan-Item``, so the writer trailers come from
the branch commits in ``merge-base(parent1, parent2)..parent2`` -- never from
the merge commit itself, from main-side commits, or from inner PR merges.

Hermetic: everything lives in ``tmp_path``, the repo sets its own
``user.name``/``user.email``, the host's global/system git config is ignored,
and nothing is pushed or fetched (no network).  The implementation under test,
``GitAdapter.writer_trailers``, does not exist yet, so these tests are red.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from plangraph.keeper.evidence_git import GitAdapter

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git not available")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit(repo: Path, subject: str, trailers: dict[str, str],
            *, email: str | None = None) -> str:
    """One ``--allow-empty`` commit; returns its sha."""
    if email:
        _git(repo, "config", "user.email", email)
    body = "".join(f"{key}: {value}\n" for key, value in trailers.items())
    _git(repo, "commit", "--allow-empty", "-q", "-m", f"{subject}\n\n{body}")
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo: Path, branch: str, plan_item: str) -> str:
    """A ``--no-ff`` merge carrying only ``Plan-Item``; returns its sha."""
    _git(repo, "merge", "--no-ff", "-q", "-m",
         f"Merge {branch}\n\nPlan-Item: {plan_item}", branch)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path, monkeypatch):
    """Ignore the host's git config; keep git inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


@pytest.fixture
def repo(tmp_path: Path, hermetic_git) -> Path:
    """A fresh repo with local identity and signing disabled."""
    path = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True,
                   capture_output=True)
    _git(path, "config", "user.name", "Test Writer")
    _git(path, "config", "user.email", "writer@example.invalid")
    _git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.fixture
def adapter() -> GitAdapter:
    return GitAdapter()


# --------------------------------------------------------------------------
# 15. a plain commit, then a --no-ff merge of a two-commit branch
# --------------------------------------------------------------------------

def test_non_merge_commit_returns_its_own_trailers(repo, adapter):
    sha = _commit(repo, "one", {"Plan-Item": "demo:A", "Writer": "mimo-v2 run1"})

    assert adapter.writer_trailers(str(repo), sha) == [
        {"Plan-Item": "demo:A", "Writer": "mimo-v2 run1"}]


def test_merge_returns_the_branch_commits_only(repo, adapter):
    _commit(repo, "base", {"Plan-Item": "demo:BASE", "Writer": "qwen-max run0"})
    _git(repo, "switch", "-q", "-c", "feat")
    f1 = _commit(repo, "f1", {"Plan-Item": "demo:A", "Writer": "mimo-v2 run1"})
    f2 = _commit(repo, "f2", {"Plan-Item": "demo:A", "Writer": "deepseek-r1 run2"})
    _git(repo, "switch", "-q", "main")
    # Main-side commit made AFTER the branch forked: outside the range.
    _commit(repo, "side", {"Plan-Item": "demo:SIDE", "Writer": "qwen-max run9"})
    merge = _merge(repo, "feat", "demo:MERGE")

    got = adapter.writer_trailers(str(repo), merge)
    assert len(got) == 2
    assert {d.get("Writer") for d in got} == {"mimo-v2 run1", "deepseek-r1 run2"}
    # Neither the merge commit (demo:MERGE) nor base/side commits leak in.
    assert {d.get("Plan-Item") for d in got} == {"demo:A"}
    assert adapter.writer_trailers(str(repo), f1) == [
        {"Plan-Item": "demo:A", "Writer": "mimo-v2 run1"}]
    assert adapter.writer_trailers(str(repo), f2) == [
        {"Plan-Item": "demo:A", "Writer": "deepseek-r1 run2"}]


# --------------------------------------------------------------------------
# 16. (D-2308) a PHASE merge over inner PR merges: leaf commits only
# --------------------------------------------------------------------------

def test_phase_merge_returns_leaf_trailers_and_skips_inner_merges(repo, adapter):
    _commit(repo, "base", {"Plan-Item": "demo:BASE"})
    _git(repo, "switch", "-q", "-c", "phase")
    # PR 1 branched off the phase branch, merged back with --no-ff.
    _git(repo, "switch", "-q", "-c", "featA")
    _commit(repo, "a1", {"Plan-Item": "demo:LEAF", "Writer": "mimo-v2 run1"})
    _commit(repo, "a2", {"Plan-Item": "demo:LEAF", "Writer": "deepseek-r1 run2"})
    _git(repo, "switch", "-q", "phase")
    _merge(repo, "featA", "demo:PR-A")
    # PR 2, then the phase branch itself merges into main.
    _git(repo, "switch", "-q", "-c", "featB")
    _commit(repo, "b1", {"Plan-Item": "demo:LEAF", "Writer": "claude-opus run1"})
    _git(repo, "switch", "-q", "phase")
    _merge(repo, "featB", "demo:PR-B")
    _git(repo, "switch", "-q", "main")
    phase = _merge(repo, "phase", "demo:PHASE")

    got = adapter.writer_trailers(str(repo), phase)
    assert len(got) == 3
    assert {d.get("Writer") for d in got} == {
        "mimo-v2 run1", "deepseek-r1 run2", "claude-opus run1"}
    # Inner PR merges (demo:PR-A/demo:PR-B) and the PHASE merge are dropped.
    assert {d.get("Plan-Item") for d in got} == {"demo:LEAF"}


# --------------------------------------------------------------------------
# 17. an OCTOPUS merge (3+ parents) is out of scope: fail closed, never
#     ignore the commits behind parents[2:]
# --------------------------------------------------------------------------

def test_octopus_merge_returns_no_writer_trailers(repo, adapter):
    _commit(repo, "base", {"Plan-Item": "demo:BASE"})
    _git(repo, "branch", "b1")
    _git(repo, "branch", "b2")
    _git(repo, "switch", "-q", "b1")
    _commit(repo, "w1", {"Plan-Item": "demo:A", "Writer": "mimo-v2 run1"})
    _git(repo, "switch", "-q", "b2")
    _commit(repo, "w2", {"Plan-Item": "demo:A", "Writer": "deepseek-r1 run2"})
    _git(repo, "switch", "-q", "main")
    _commit(repo, "side", {"Plan-Item": "demo:SIDE", "Writer": "qwen-max run9"})
    # One `git merge` of two branches at once: three parents.
    _git(repo, "merge", "--no-ff", "-q", "-m",
         "Octopus\n\nPlan-Item: demo:OCTOPUS", "b1", "b2")
    octopus = _git(repo, "rev-parse", "HEAD")
    assert len(_git(repo, "rev-list", "--parents", "-n", "1",
                    octopus).split()) == 4

    # A writer behind parents[2:] must never be silently skipped: the adapter
    # cannot tell who wrote an octopus, so the keeper gets [] and fails closed.
    assert adapter.writer_trailers(str(repo), octopus) == []
