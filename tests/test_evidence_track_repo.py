"""Per-track fallback evidence repo (plangraph:KEEPER-EVIDENCE-TRACK-REPO).

A phase-0 RIN item is project ``plangraph`` / track ``ROUTING`` while its commit
lands in the plangraph-data repo: a keeper.json ``repos``/``ci`` entry keyed
``<project>/<track>`` must act as a FALLBACK repo for exactly that project+track
and for no other item.  The fakes in ``evidence_fakes`` are extended (never
edited) with per-key repo paths, commit presence, target refs and CI modes, so
both commit verifiers run without git, network or filesystem.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from evidence_fakes import SHA, FakeCI, FakeGit, make_ctx, verify
from keeper_util import World, defn
from plangraph.keeper import evidence
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.evidence_repo import repo_key
from test_keeper_commit_closes2 import DID, _batch_file, _decisions_adapter, _git_cmd, git_repo

PROJECT = "plangraph"
TRACK = "ROUTING"
TRACK_KEY = f"{PROJECT}/{TRACK}"
ITEM = {"project": PROJECT, "track": TRACK, "id": "T1", "key": "plangraph:T1"}
ITEM_OTHER = {**ITEM, "track": "OTHER"}
PRIMARY_REPO = "/repos/plangraph"
TRACK_REPO = "/repos/plangraph-data"
PRIMARY_REF = "main"
TRACK_REF = "routing-main"


class TrackGit(FakeGit):
    """FakeGit with per-key repo paths, commit presence and target refs."""

    def __init__(self, repos, present, refs=None, exists_error=None, **over):
        over.setdefault("trailers", {"Plan-Item": ITEM["key"]})
        super().__init__(**over)
        self.repos = dict(repos)
        self.present = dict(present)
        self.refs = dict(refs or {})
        self.exists_error = exists_error
        self.exists_calls = []
        self.repo_ref_calls = []
        self.ancestor_calls = []
        self.trailer_calls = []

    def repo_path(self, project):
        self.repo_path_calls.append(project)
        if self.error:
            raise self.error
        return self.repos.get(project)

    def commit_exists(self, repo, sha):
        self.exists_calls.append((repo, sha))
        if self.exists_error:
            raise self.exists_error
        return bool(self.present.get(repo))

    def repo_ref(self, project=None, *args, **kwargs):
        self.repo_ref_calls.append(project)
        return self.refs.get(project, PRIMARY_REF)

    def is_ancestor(self, repo, sha, ref):
        self.ancestor_calls.append((repo, sha, ref))
        return super().is_ancestor(repo, sha, ref)

    def trailers(self, repo, sha):
        self.trailer_calls.append((repo, sha))
        return super().trailers(repo, sha)


class TrackCI(FakeCI):
    """FakeCI with per-key CI modes and call recording."""

    def __init__(self, modes, **over):
        super().__init__(**over)
        self.modes = dict(modes)
        self.mode_calls = []

    def mode(self, project):
        self.mode_calls.append(project)
        return self.modes.get(project)


def _git(primary_has=False, track_has=True, track_registered=True,
         trailers=None, exists_error=None, **over):
    repos = {PROJECT: PRIMARY_REPO}
    if track_registered:
        repos[TRACK_KEY] = TRACK_REPO
    return TrackGit(
        repos,
        {PRIMARY_REPO: primary_has, TRACK_REPO: track_has},
        refs={PROJECT: PRIMARY_REF, TRACK_KEY: TRACK_REF},
        exists_error=exists_error,
        trailers={"Plan-Item": ITEM["key"]} if trailers is None else trailers,
        **over,
    )


def _ci(modes=None):
    return TrackCI({PROJECT: "none", TRACK_KEY: "none"} if modes is None else modes)


def _run(kind, item=ITEM, git=None, ci=None):
    return verify(kind, {"commit": SHA}, make_ctx(git=git or _git(), ci=ci or _ci()),
                  item=item)


# --------------------------------------------------------------------------- #
# repo_key: the fallback rule and its fail-safe edges
# --------------------------------------------------------------------------- #

def test_repo_key_prefers_track_repo_when_only_it_holds_commit():
    assert repo_key(ITEM, make_ctx(git=_git()), SHA) == TRACK_KEY


def test_repo_key_keeps_project_when_commit_is_in_both_repos():
    assert repo_key(ITEM, make_ctx(git=_git(primary_has=True)), SHA) == PROJECT


def test_repo_key_keeps_project_when_commit_is_nowhere():
    git = _git(primary_has=False, track_has=False)
    assert repo_key(ITEM, make_ctx(git=git), SHA) == PROJECT


def test_repo_key_keeps_project_when_track_repo_unregistered():
    assert repo_key(ITEM, make_ctx(git=_git(track_registered=False)), SHA) == PROJECT


def test_repo_key_keeps_project_for_item_without_track():
    git = _git()
    item = {"project": PROJECT, "id": "T1", "key": ITEM["key"]}
    assert repo_key(item, make_ctx(git=git), SHA) == PROJECT


def test_repo_key_never_raises_when_commit_exists_raises():
    git = _git(exists_error=RuntimeError("git exploded"))
    assert repo_key(ITEM, make_ctx(git=git), SHA) == PROJECT


def test_repo_key_keeps_project_without_git():
    assert repo_key(ITEM, make_ctx(git=None), SHA) == PROJECT


# --------------------------------------------------------------------------- #
# commit+ci: the track repo is a fallback, and only for the exact commit
# --------------------------------------------------------------------------- #

def test_commit_ci_track_repo_fallback_passes():
    git, ci = _git(), _ci()
    res = _run("commit+ci", git=git, ci=ci)
    assert res.ok is True, res.detail
    assert SHA in res.detail
    assert git.repo_path_calls[-1] == TRACK_KEY
    assert git.repo_ref_calls == [TRACK_KEY]
    assert ci.mode_calls == [TRACK_KEY]


def test_commit_ci_other_track_without_override_fails():
    res = _run("commit+ci", item=ITEM_OTHER)
    assert res.ok is False
    assert "does not exist" in res.detail


def test_commit_ci_commit_in_both_repos_uses_primary():
    git, ci = _git(primary_has=True), _ci({PROJECT: "none", TRACK_KEY: "owner/repo"})
    res = _run("commit+ci", git=git, ci=ci)
    assert res.ok is True, res.detail
    assert "(ci: none)" in res.detail
    assert ci.mode_calls == [PROJECT]
    assert git.repo_ref_calls == [PROJECT]


def test_commit_ci_override_without_commit_fails():
    git = _git(primary_has=False, track_has=False)
    res = _run("commit+ci", git=git)
    assert res.ok is False
    assert "does not exist" in res.detail


def test_commit_ci_override_commit_without_trailer_fails():
    res = _run("commit+ci", git=_git(trailers={}))
    assert res.ok is False
    assert "Plan-Item" in res.detail


def test_commit_ci_commit_exists_error_fails_without_raising():
    res = _run("commit+ci", git=_git(exists_error=RuntimeError("git exploded")))
    assert res.ok is False
    assert "RuntimeError" in res.detail


# --------------------------------------------------------------------------- #
# legacy commit: same fallback rule, no trailer and no CI to weaken
# --------------------------------------------------------------------------- #

def test_commit_track_repo_fallback_passes():
    git = _git()
    res = _run("commit", git=git)
    assert res.ok is True, res.detail
    assert SHA in res.detail
    assert f"is an ancestor of {TRACK_REF}" in res.detail
    assert git.repo_ref_calls == [TRACK_KEY]


def test_commit_other_track_without_override_fails():
    res = _run("commit", item=ITEM_OTHER)
    assert res.ok is False
    assert "does not exist" in res.detail


def test_commit_commit_in_both_repos_uses_primary():
    git = _git(primary_has=True)
    res = _run("commit", git=git)
    assert res.ok is True, res.detail
    assert f"is an ancestor of {PRIMARY_REF}" in res.detail
    assert git.repo_ref_calls == [PROJECT]


def test_commit_override_without_commit_fails():
    git = _git(primary_has=False, track_has=False)
    res = _run("commit", git=git)
    assert res.ok is False
    assert "does not exist" in res.detail


def test_commit_commit_exists_error_fails_without_raising():
    res = _run("commit", git=_git(exists_error=RuntimeError("git exploded")))
    assert res.ok is False
    assert "RuntimeError" in res.detail


# ---- F1: a fallback commit's ancestry floor fails closed, never passes ------

FLOOR = "f" * 40


def test_commit_ci_fallback_floor_unknown_to_track_repo_fails_closed():
    ctx = make_ctx(git=_git(descends=False), ci=_ci(), bind_floor_required=True, bind_floor_sha=FLOOR)
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "does not descend" in res.detail


def test_commit_ci_fallback_floor_git_error_fails_closed():
    ctx = make_ctx(git=_git(floor_error=RuntimeError("floor gone")), ci=_ci(),
                   bind_floor_required=True, bind_floor_sha=FLOOR)
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "git error checking the ancestry floor" in res.detail


# ---- F3/F4: no input type may raise; no <project>/<track> key collisions ----

def test_repo_key_never_raises_for_non_dict_item():
    assert repo_key(["x"], make_ctx(git=_git()), SHA) is None
    assert repo_key(None, make_ctx(git=_git()), SHA) is None


def test_repo_key_never_raises_for_empty_item():
    assert repo_key({}, make_ctx(git=_git()), SHA) is None


def test_repo_key_ignores_track_with_slash():
    item = {"project": "a", "track": "b/c", "id": "T1", "key": "a:T1"}
    git = TrackGit({"a": PRIMARY_REPO, "a/b/c": TRACK_REPO}, {PRIMARY_REPO: False, TRACK_REPO: True},
                   refs={"a": PRIMARY_REF, "a/b/c": TRACK_REF})
    assert repo_key(item, make_ctx(git=git), SHA) == "a"


def test_repo_key_ignores_project_with_slash():
    item = {"project": "a/b", "track": "c", "id": "T1", "key": "a/b:T1"}
    git = TrackGit({"a/b": PRIMARY_REPO, "a/b/c": TRACK_REPO}, {PRIMARY_REPO: False, TRACK_REPO: True},
                   refs={"a/b": PRIMARY_REF, "a/b/c": TRACK_REF})
    assert repo_key(item, make_ctx(git=git), SHA) == "a/b"


def test_repo_key_ignores_empty_track():
    git = _git()
    assert repo_key({**ITEM, "track": ""}, make_ctx(git=git), SHA) == PROJECT


def test_repo_key_ignores_non_str_track():
    git = _git()
    assert repo_key({**ITEM, "track": 7}, make_ctx(git=git), SHA) == PROJECT


# ---- F5: fallback pass/fail edges (ancestry, CI, exact call trail) ----------

def test_commit_ci_fallback_present_but_not_ancestor_of_track_ref_fails():
    res = _run("commit+ci", git=_git(ancestor=False))
    assert res.ok is False
    assert f"is not an ancestor of {TRACK_REF}" in res.detail


def test_commit_fallback_present_but_not_ancestor_of_track_ref_fails():
    res = _run("commit", git=_git(ancestor=False))
    assert res.ok is False
    assert f"is not an ancestor of {TRACK_REF}" in res.detail


def test_commit_ci_track_registered_but_absent_from_ci_fails():
    res = _run("commit+ci", git=_git(), ci=_ci({PROJECT: "none"}))
    assert res.ok is False
    assert "no CI configured" in res.detail


def test_commit_ci_fallback_pass_calls_only_the_track_repo():
    git, ci = _git(), _ci()
    res = _run("commit+ci", git=git, ci=ci)
    assert res.ok is True, res.detail
    assert git.exists_calls[-1] == (TRACK_REPO, SHA)
    assert git.ancestor_calls == [(TRACK_REPO, SHA, TRACK_REF)]
    assert git.trailer_calls == [(TRACK_REPO, SHA)]
    assert git.repo_ref_calls == [TRACK_KEY]
    assert ci.mode_calls == [TRACK_KEY]


# ---- F2/F5: store close with a fallback commit row and its Closes token -----

SKEY = "demo:A"
STRACK = "demo/ROUTING"


def _track_repos(tmp_path, track_repo, ref="main"):
    """Both keys registered: an empty primary (holds no commit) + the track repo."""
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    return {"demo": {"path": str(primary), "ref": "main"}, STRACK: {"path": str(track_repo), "ref": ref}}


def _store_world(tmp_path, repos, cell):
    """World with a ROUTING tier-none ``demo:A`` item (decision+commit kinds), set up
    like ``test_keeper_commit_closes2`` whose ``_decisions_adapter``/``_batch_file`` are reused."""
    w = World(tmp_path)
    w.store.verifiers = dict(evidence.VERIFIERS)
    w.store.create_items(w.p("router"), [
        defn("A", [], evidence_kinds=["decision", "commit"], review_tier="none", track="ROUTING"),
        defn("GOAL", ["A"], kind="goal"),
    ])
    w.store.ctx = SimpleNamespace(decisions=_decisions_adapter(tmp_path, cell), git=GitAdapter(repos))
    w.batch_ref = _batch_file(tmp_path)
    w.store.batch_roots = [str(tmp_path / "plan")]
    return w


def test_store_close_fallback_commit_with_matching_token_closes(tmp_path, git_repo):
    feat = git_repo.feature_sha
    w = _store_world(tmp_path, _track_repos(tmp_path, git_repo.path),
                     f"Closes: {SKEY}@{feat}")
    w.store.set_legacy_state(w.p("router"), SKEY, "in_review")
    w.store.add_evidence_router(w.p("router"), SKEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), SKEY, "commit", {"sha": feat})
    res = w.store.close_on_decision(w.p("router"), SKEY, w.batch_ref)
    assert res["state"] == "done"
    assert res["decision_ids"] == [DID]
    w.close()


def test_store_close_fallback_commit_with_foreign_token_refuses(tmp_path, git_repo):
    feat = git_repo.feature_sha
    foreign = "e" * 40
    w = _store_world(tmp_path, _track_repos(tmp_path, git_repo.path),
                     f"Closes: {SKEY}@{foreign}")
    w.store.set_legacy_state(w.p("router"), SKEY, "in_review")
    w.store.add_evidence_router(w.p("router"), SKEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), SKEY, "commit", {"sha": feat})
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), SKEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert (f"Closes token {SKEY}@{foreign} does not match"
            f" the verified commit evidence ['{feat}']") in exc.value.msg
    w.close()


def test_store_close_fallback_commit_not_merged_names_track_ref(tmp_path, git_repo):
    repo = git_repo.path
    _git_cmd(repo, "branch", "routing-main")
    _git_cmd(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git_cmd(repo, "add", "side.txt")
    _git_cmd(repo, "commit", "-q", "-m", "side commit")
    side = _git_cmd(repo, "rev-parse", "HEAD")
    _git_cmd(repo, "checkout", "-q", "main")
    w = _store_world(tmp_path, _track_repos(tmp_path, repo, ref="routing-main"),
                     f"Closes: {SKEY}@{side}")
    w.store.set_legacy_state(w.p("router"), SKEY, "in_review")
    w.store.add_evidence_router(w.p("router"), SKEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), SKEY, "commit", {"sha": side})
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), SKEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert exc.value.msg == f"commit {side} not merged into routing-main"
    w.close()
