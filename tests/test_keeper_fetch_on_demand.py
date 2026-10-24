"""W19 PG-FETCH-ON-DEMAND: the keeper fetches stale clones on demand.

Real git in tmp_path: a bare "remote" repo, a keeper clone registered with
ref ``origin/main``, and a second clone that pushes new commits. The keeper
clone must see just-pushed shas after one adapter call, without a manual
fetch and without touching the verifiers.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from plangraph.keeper.evidence_git import GitAdapter, LegacyCommitVerifier
from plangraph.keeper.evidence_types import Context

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)

_ENV = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

# Valid but (practically) never-existing shas for miss/rate-limit tests.
UNKNOWN_A = "aa" * 20
UNKNOWN_B = "bb" * 20


def _git(cwd, *args):
    env = dict(os.environ, **_ENV)
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _local_exists(repo, sha):
    proc = subprocess.run(
        ["git", "cat-file", "-e", "--end-of-options", f"{sha}^{{commit}}"],
        cwd=str(repo),
        capture_output=True,
    )
    return proc.returncode == 0


@pytest.fixture
def world(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "f.txt").write_text("one", encoding="utf-8")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-q", "-m", "first")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "-q", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    keeper = tmp_path / "keeper"
    _git(tmp_path, "clone", "-q", str(remote), str(keeper))
    writer = tmp_path / "writer"
    _git(tmp_path, "clone", "-q", str(remote), str(writer))
    return {"remote": remote, "keeper": keeper, "writer": writer}


def _push(writer, name, text):
    (writer / name).write_text(text, encoding="utf-8")
    _git(writer, "add", name)
    _git(writer, "commit", "-q", "-m", f"add {name}")
    _git(writer, "push", "-q", "origin", "main")
    return _git(writer, "rev-parse", "HEAD")


def _adapter(world, **kwargs):
    return GitAdapter(
        {"demo": {"path": str(world["keeper"]), "ref": "origin/main"}}, **kwargs
    )


def _fetch_spy(adapter):
    calls = []
    orig = adapter._run

    def spy(repo, args, **kwargs):
        if args and args[0] == "fetch":
            calls.append(tuple(args))
        return orig(repo, args, **kwargs)

    adapter._run = spy
    return calls


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_commit_exists_fetches_on_demand(world):
    sha = _push(world["writer"], "n1.txt", "new")
    assert not _local_exists(world["keeper"], sha)
    assert _adapter(world).commit_exists(str(world["keeper"]), sha) is True


def test_is_ancestor_fetches_on_demand(world):
    sha = _push(world["writer"], "n1.txt", "new")
    adapter = _adapter(world)
    assert adapter.is_ancestor(str(world["keeper"]), sha, "origin/main") is True


def test_rate_limit_one_fetch_per_60s(world):
    clock = _Clock()
    adapter = _adapter(world, clock=clock)
    calls = _fetch_spy(adapter)
    keeper = str(world["keeper"])
    assert adapter.commit_exists(keeper, UNKNOWN_A) is False
    assert len(calls) == 1
    # A commit pushed after that fetch is still invisible inside the window.
    sha = _push(world["writer"], "n2.txt", "later")
    assert adapter.commit_exists(keeper, UNKNOWN_B) is False
    assert adapter.commit_exists(keeper, sha) is False
    assert len(calls) == 1
    # Past the window the next miss fetches and the sha resolves.
    clock.t += 61.0
    assert adapter.commit_exists(keeper, sha) is True
    assert len(calls) == 2


def test_local_ref_never_fetches(world):
    adapter = GitAdapter(
        {"demo": {"path": str(world["keeper"]), "ref": "main"}}
    )
    calls = _fetch_spy(adapter)
    keeper = str(world["keeper"])
    assert adapter.commit_exists(keeper, UNKNOWN_A) is False
    assert adapter.is_ancestor(keeper, UNKNOWN_A, "main") is False
    assert calls == []


def test_fetch_failure_returns_false(world):
    shutil.rmtree(world["remote"])
    adapter = _adapter(world)
    assert adapter.commit_exists(str(world["keeper"]), UNKNOWN_A) is False
    assert adapter.is_ancestor(str(world["keeper"]), UNKNOWN_A, "origin/main") is False


def test_legacy_verifier_ok_on_just_pushed_sha(world):
    sha = _push(world["writer"], "n1.txt", "new")
    ctx = Context(git=_adapter(world))
    result = LegacyCommitVerifier().verify({"project": "demo"}, {"sha": sha}, ctx)
    assert result.ok is True
