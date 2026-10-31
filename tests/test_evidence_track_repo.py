"""Per-track fallback evidence repo (plangraph:KEEPER-EVIDENCE-TRACK-REPO).

A phase-0 RIN item is project ``plangraph`` / track ``ROUTING`` while its commit
lands in the plangraph-data repo: a keeper.json ``repos``/``ci`` entry keyed
``<project>/<track>`` must act as a FALLBACK repo for exactly that project+track
and for no other item.  The fakes in ``evidence_fakes`` are extended (never
edited) with per-key repo paths, commit presence, target refs and CI modes, so
both commit verifiers run without git, network or filesystem.
"""

from __future__ import annotations

from evidence_fakes import SHA, FakeCI, FakeGit, make_ctx, verify
from plangraph.keeper.evidence_repo import repo_key

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
         trailers=None, exists_error=None):
    repos = {PROJECT: PRIMARY_REPO}
    if track_registered:
        repos[TRACK_KEY] = TRACK_REPO
    return TrackGit(
        repos,
        {PRIMARY_REPO: primary_has, TRACK_REPO: track_has},
        refs={PROJECT: PRIMARY_REF, TRACK_KEY: TRACK_REF},
        exists_error=exists_error,
        trailers={"Plan-Item": ITEM["key"]} if trailers is None else trailers,
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
