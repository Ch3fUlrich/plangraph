"""Seat coverage track repo resolution (plangraph:KEEPER-COVERAGE-TRACK-REPO)."""
from __future__ import annotations

import pytest

from evidence_fakes import FakeGit, make_ctx
from keeper_seat_helpers import (
    MERGE_SHA,
    SHA_A,
    SHA_B,
    _cov,
    to_review_merge,
    to_review_with_commits,
    w,  # noqa: F401 - the review world fixture
    writer,
)
from keeper_util import seed
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_repo import track_key


class TrackFakeGit(FakeGit):
    """FakeGit extended so repo_path("<project>/<track>") returns a track repo

    and commits can live in only one repo (or both when primary_commits is set).
    """

    def __init__(self, *a, track_repo="/repos/demo-track", track_commits=(),
                 primary_commits=None, raising_repos=(), raising_on_call=None,
                 trailers_by_repo_sha=None, trailers_error=None, **kw):
        super().__init__(*a, **kw)
        self.track_repo = track_repo
        self.track_commits = set(track_commits or ())
        self.primary_commits = set(primary_commits) if primary_commits is not None else None
        self.raising_repos = set(raising_repos or ())
        self.raising_on_call = dict(raising_on_call or {})
        self._key_calls: dict[str, int] = {}
        self.trailers_by_repo_sha = dict(trailers_by_repo_sha or {})
        self.trailers_error = trailers_error
        self.writer_trailers_calls: list[tuple[str | None, str]] = []

    def repo_path(self, project):
        self.repo_path_calls.append(project)
        self._key_calls[project] = n = self._key_calls.get(project, 0) + 1
        if project in self.raising_repos or self.raising_on_call.get(project) == n:
            raise RuntimeError(f"repo_path error for {project}")
        return self.track_repo if "/" in project else super().repo_path(project)

    def commit_exists(self, repo, sha):
        if repo == self.track_repo:
            return sha in self.track_commits
        if self.primary_commits is not None:
            return sha in self.primary_commits
        return sha not in self.track_commits and self.exists

    def trailers(self, repo, sha):
        if (repo, sha) in self.trailers_by_repo_sha:
            return dict(self.trailers_by_repo_sha[(repo, sha)])
        return super().trailers(repo, sha) if self.commit_exists(repo, sha) else {}

    def writer_trailers(self, repo, sha):
        self.writer_trailers_calls.append((repo, sha))
        if self.trailers_error:
            raise self.trailers_error
        if (repo, sha) in self.trailers_by_repo_sha:
            return [dict(self.trailers_by_repo_sha[(repo, sha)])]
        return super().writer_trailers(repo, sha) if self.commit_exists(repo, sha) else []


class GitWithoutCommitExists(TrackFakeGit):
    """Adapter lacking commit_exists to pin fallback behaviour."""

    def __init__(self, *a, has_trailers=True, **kw):
        super().__init__(*a, **kw)
        self.has_trailers = has_trailers

    def __getattribute__(self, name):
        if name == "commit_exists":
            raise AttributeError("commit_exists")
        return super().__getattribute__(name)

    def trailers(self, repo, sha):
        if (repo, sha) in self.trailers_by_repo_sha:
            return dict(self.trailers_by_repo_sha[(repo, sha)])
        return FakeGit.trailers(self, repo, sha) if self.has_trailers else {}

    def writer_trailers(self, repo, sha):
        self.writer_trailers_calls.append((repo, sha))
        if self.trailers_error:
            raise self.trailers_error
        if (repo, sha) in self.trailers_by_repo_sha:
            return [dict(self.trailers_by_repo_sha[(repo, sha)])]
        return FakeGit.writer_trailers(self, repo, sha) if self.has_trailers else []


def test_track_repo_merge_commit_hand_edit_is_anthropic(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    git = TrackFakeGit(trailers_by_sha={MERGE_SHA: {"Plan-Item": "demo:A"}},
                       branch_trailers_by_sha={MERGE_SHA: [{"Hand-Edit": "D-3360"}]},
                       track_commits={MERGE_SHA})
    to_review_merge(w, key, git, sha=MERGE_SHA)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["anthropic"]
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic1"), key, True, "hand edit")
    assert exc.value.code == "same_family" and "anthropic" in exc.value.msg
    assert w.store.verdict(w.p("rGoogle"), key, True, "covering")["state"] == "done"


def test_track_repo_writer_trailer_resolves_family(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    git = TrackFakeGit(trailers_by_sha={SHA_A: writer("mimo-v2.6-pro run-47db")}, track_commits={SHA_A})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2.6-pro run-47db")}, git=git)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["xiaomi"]
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), key, True, "same")
    assert exc.value.code == "same_family" and "xiaomi" in exc.value.msg
    assert w.store.verdict(w.p("rAnthropic1"), key, True, "covering")["state"] == "done"


def test_primary_repo_row_unchanged(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    git = TrackFakeGit(trailers_by_sha={SHA_A: writer("mimo-v2 run1")}, track_commits=set())
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=git)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["xiaomi"]
    assert w.store.verdict(w.p("rAnthropic1"), key, True, "covering")["state"] == "done"


def test_track_repo_unregistered_and_commit_not_in_primary_refuses(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    reg = TrackFakeGit(trailers_by_sha={SHA_A: writer("mimo-v2.6-pro run1")}, track_commits={SHA_A})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2.6-pro run1")}, git=reg)
    w.store.ctx = make_ctx(git=TrackFakeGit(track_repo=None, track_commits={SHA_A}))
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "demo/data" in exc.value.msg
    assert "(resolved to primary 'demo'; track 'demo/data' not used)" in exc.value.msg


def test_track_repo_path_raising_refuses_writer_unknown(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    reg = TrackFakeGit(trailers_by_sha={SHA_A: writer("mimo-v2.6-pro run1")}, track_commits={SHA_A})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2.6-pro run1")}, git=reg)
    w.store.ctx = make_ctx(git=TrackFakeGit(track_commits={SHA_A}, raising_repos={"demo/data"}))
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "demo/data" in exc.value.msg
    w.store.ctx = make_ctx(git=TrackFakeGit(track_commits={SHA_A}, raising_on_call={"demo/data": 2}))
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown" and "demo/data" in exc.value.msg


def test_commit_in_both_repos_reads_primary(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    primary_repo = "/repos/routing"
    track_repo = "/repos/demo-track"
    git = TrackFakeGit(
        primary_commits={SHA_A},
        track_repo=track_repo,
        track_commits={SHA_A},
        trailers_by_repo_sha={
            (primary_repo, SHA_A): writer("mimo-v2 run1"),
            (track_repo, SHA_A): writer("claude-3-7-sonnet-20250219 run2"),
        },
    )
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=git)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["xiaomi"]
    assert (primary_repo, SHA_A) in git.writer_trailers_calls
    assert (track_repo, SHA_A) not in git.writer_trailers_calls


def test_commit_in_neither_repo_refuses_naming_track_key(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    reg = TrackFakeGit(track_commits={SHA_A}, trailers_by_sha={SHA_A: writer("mimo-v2 run1")})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=reg)
    w.store.ctx = make_ctx(git=TrackFakeGit(primary_commits=set(), track_commits=set()))
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "demo/data" in exc.value.msg
    assert "(resolved to primary 'demo'; track 'demo/data' not used)" in exc.value.msg


def test_refusals_with_track_name_track_key_for_all_writer_unknown_forms(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    reg = TrackFakeGit(track_commits={SHA_A}, trailers_by_sha={SHA_A: writer("mimo-v2 run1")})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=reg)

    # 1. Trailer read error
    err_git = TrackFakeGit(primary_commits={SHA_A}, track_commits=set(),
                           trailers_error=RuntimeError("git show failed"))
    w.store.ctx = make_ctx(git=err_git)
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "cannot read writer trailers for" in exc.value.msg
    assert "(resolved to primary 'demo'; track 'demo/data' not used)" in exc.value.msg

    # 2. Neither Writer nor Hand-Edit
    empty_trailer_git = TrackFakeGit(
        primary_commits={SHA_A}, track_commits=set(),
        trailers_by_repo_sha={("/repos/routing", SHA_A): {"Plan-Item": "demo:A"}},
    )
    w.store.ctx = make_ctx(git=empty_trailer_git)
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "has neither Writer nor Hand-Edit" in exc.value.msg
    assert "(resolved to primary 'demo'; track 'demo/data' not used)" in exc.value.msg


def test_git_adapter_without_commit_exists_pinned(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    reg = TrackFakeGit(trailers_by_sha={SHA_A: writer("mimo-v2 run1")})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=reg)

    # Subcase A: commit present with trailers in primary repo -> primary read
    git_no_exists = GitWithoutCommitExists(has_trailers=True,
                                           trailers_by_sha={SHA_A: writer("mimo-v2 run1")})
    w.store.ctx = make_ctx(git=git_no_exists)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["xiaomi"]

    # Subcase B: commit not present with trailers in primary repo -> refuses naming track key
    git_no_trailers = GitWithoutCommitExists(has_trailers=False)
    w.store.ctx = make_ctx(git=git_no_trailers)
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "demo/data" in exc.value.msg
    assert "(resolved to primary 'demo'; track 'demo/data' not used)" in exc.value.msg


@pytest.mark.parametrize("bad_item", [None, ["not", "a", "dict"], "not-a-dict", 123])
def test_non_dict_item_or_missing_project_refuses(w, bad_item):
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, bad_item, w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert exc.value.msg == "cannot resolve project repository"


@pytest.mark.parametrize("bad_item", [
    {"project": "demo", "epoch": 1},
    {"project": "demo", "key": "k"},
    {"project": "demo", "key": None, "epoch": 1},
    {"project": "demo", "key": "k", "epoch": None},
    {}, {"track": "data"},
])
def test_item_lacking_key_or_epoch_refuses(w, bad_item):
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, bad_item, w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert exc.value.msg == "item lacks key/epoch"


def test_decision_only_item_without_project_returns_empty(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    item = dict(w.store._item(key))
    item.pop("project", None)
    assert _cov().writer_families(w.store, item, w.store.ctx) == []


@pytest.mark.parametrize("bad_project", [None, 123, "", ["demo"]])
def test_item_with_commit_rows_and_no_project_refuses_naming_project(w, bad_project):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})
    item = dict(w.store._item(key))
    if bad_project is None:
        item.pop("project", None)
    else:
        item["project"] = bad_project
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, item, w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert exc.value.msg == "item project missing or not a string"
    assert "project" in exc.value.msg


def test_track_item_repo_path_raising_on_primary_names_track_key(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    reg = TrackFakeGit(track_commits={SHA_A}, trailers_by_sha={SHA_A: writer("mimo-v2 run1")})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=reg)
    git = TrackFakeGit(track_commits={SHA_A}, raising_repos={"demo"})
    w.store.ctx = make_ctx(git=git)
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "demo/data" in exc.value.msg
    assert "cannot resolve project repository (resolved to primary 'demo'; track 'demo/data' not used)" in exc.value.msg


def test_track_item_repo_path_returning_none_on_primary_names_track_key(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    reg = TrackFakeGit(track_commits={SHA_A}, trailers_by_sha={SHA_A: writer("mimo-v2 run1")})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=reg)
    git = TrackFakeGit(repo=None, track_commits=set())
    w.store.ctx = make_ctx(git=git)
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert "demo/data" in exc.value.msg
    assert "no repo registered for project 'demo' (resolved to primary 'demo'; track 'demo/data' not used)" in exc.value.msg


def test_mixed_rows_primary_and_track_both_families_returned(w):
    (key,) = seed(w, [("A", [])], track="data", evidence_kinds=("commit+ci",))
    mixed = {SHA_A: writer("mimo-v2 run1"), SHA_B: writer("claude-3-7-sonnet-20250219 run2")}
    # SHA_B in track_commits; SHA_A not (so SHA_A is in primary).
    git = TrackFakeGit(track_commits={SHA_B}, trailers_by_sha=mixed)
    to_review_with_commits(w, key, mixed, git=git)
    families = _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert sorted(families) == ["anthropic", "xiaomi"]


def test_primary_path_messages_pinned(w):
    (key,) = seed(w, [("A", [])], track=None, evidence_kinds=("commit+ci",))
    git_good = FakeGit(trailers_by_sha={SHA_A: writer("mimo-v2 run1")})
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")}, git=git_good)

    # Subcase A: repo_path raises -> "cannot resolve project repository"
    class RaisingPathGit(FakeGit):
        def repo_path(self, project):
            raise RuntimeError("disk error")

    w.store.ctx = make_ctx(git=RaisingPathGit())
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert exc.value.msg == "cannot resolve project repository"

    # Subcase B: repo_path returns None/falsy -> "no repo registered for project 'demo'"
    w.store.ctx = make_ctx(git=FakeGit(repo=None))
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert exc.value.msg == "no repo registered for project 'demo'"


@pytest.mark.parametrize("rogue_track", ["x/../y", "data/extra", "../escape", "/root"])
def test_rogue_track_key_never_selects_repo(w, rogue_track):
    (key,) = seed(w, [("A", [])], track=rogue_track, evidence_kinds=("commit+ci",))
    rogue_repo = f"/repos/rogue-{rogue_track}"
    git = TrackFakeGit(
        primary_commits=set(),
        track_repo=rogue_repo,
        track_commits={SHA_A},
        trailers_by_sha={SHA_A: writer("mimo-v2 run1")},
    )
    epoch = w.store.claim(w.p("claimant"), key, lease_s=7200)["epoch"]
    w.conn.execute(
        "INSERT INTO evidence (key, epoch, kind, ref, verified) "
        "VALUES (?, ?, 'commit+ci', ?, 1)",
        (key, epoch, f'{{"commit": "{SHA_A}"}}'),
    )
    w.store.ctx = make_ctx(git=git)
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown"
    assert f"demo/{rogue_track}" not in git.repo_path_calls
    assert exc.value.msg == f"commit {SHA_A} names no writer"


@pytest.mark.parametrize("item,expected", [
    (None, None), ("not a dict", None), (123, None), ([], None), ({}, None),
    ({"project": "demo"}, None), ({"track": "data"}, None),
    ({"project": None, "track": "data"}, None), ({"project": "demo", "track": None}, None),
    ({"project": 123, "track": "data"}, None), ({"project": "demo", "track": 123}, None),
    ({"project": "", "track": "data"}, None), ({"project": "demo", "track": ""}, None),
    ({"project": "demo/bad", "track": "data"}, None),
    ({"project": "demo", "track": "data/bad"}, None),
    ({"project": "demo", "track": "a" * 65}, None),
    ({"project": "demo", "track": "data"}, "demo/data"),
    ({"project": "proj-1.0_x", "track": "track-2.1_y"}, "proj-1.0_x/track-2.1_y"),
    ({"project": "demo", "track": "a" * 64}, f"demo/{'a' * 64}"),
])
def test_track_key_helper(item, expected):
    assert track_key(item) == expected


def test_track_key_never_raises_on_broken_mapping():
    class ThrowingMapping(dict):
        def get(self, k, default=None):
            raise RuntimeError("broken")

    assert track_key(ThrowingMapping()) is None

