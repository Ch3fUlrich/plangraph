"""Tests for commit-gated decision closes (D-505).

1. Closes: tokens may carry a commit (<project:ID>@<sha>, 7..40 hex chars).
2. Configured target ref per repo in config.py and evidence_git.py.
3. Tier 'none' items with evidence_kinds == ['decision', 'commit'].
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, to_claimed
from plangraph.keeper import config, evidence
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter, valid_ref

DID = "D-505"
KEY = "demo:A"
HEADER = "| id | project | decision | by | why / evidence |\n|---|---|---|---|---|\n"


def _decisions_adapter(tmp_path: Path, cell: str, did: str = DID) -> DecisionsAdapter:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "DECISIONS.md"
    path.write_text(HEADER + f"| {did} | demo | {cell} | router | tested |\n", encoding="utf-8")
    return DecisionsAdapter(path)


def _batch_file(tmp_path: Path) -> str:
    root = tmp_path / "plan"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "batch.md"
    path.write_text("batch content\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{path}@{digest}"


def _git_cmd(repo_path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path):
    """Temp git repo with two branches: 'main' and 'feature'."""
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git_cmd(repo, "config", "user.email", "operator@example.invalid")
    _git_cmd(repo, "config", "user.name", "Operator")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git_cmd(repo, "add", "base.txt")
    _git_cmd(repo, "commit", "-q", "-m", "main commit")
    _git_cmd(repo, "branch", "-M", "main")
    main_sha = _git_cmd(repo, "rev-parse", "HEAD")

    _git_cmd(repo, "checkout", "-q", "-b", "feature")
    (repo / "feat.txt").write_text("feature\n", encoding="utf-8")
    _git_cmd(repo, "add", "feat.txt")
    _git_cmd(repo, "commit", "-q", "-m", "feature commit")
    feature_sha = _git_cmd(repo, "rev-parse", "HEAD")
    _git_cmd(repo, "checkout", "-q", "main")
    return SimpleNamespace(path=repo, main_sha=main_sha, feature_sha=feature_sha)


def _make_config(tmp_path: Path, data: dict) -> config.Config:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "keeper.json"
    content = {"db": "k.db", "bind": "127.0.0.1:8765"}
    content.update(data)
    path.write_text(json.dumps(content), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


def _world(tmp_path: Path, git_repo=None, kinds=("decision", "commit"),
           ref="main", cell=f"Closes: {KEY}") -> World:
    w = World(tmp_path)
    w.store.verifiers = dict(evidence.VERIFIERS)
    w.store.create_items(w.p("router"), [
        defn("A", [], evidence_kinds=list(kinds), review_tier="none"),
        defn("GOAL", ["A"], kind="goal"),
    ])
    adapter = _decisions_adapter(tmp_path, cell)
    git = GitAdapter({"demo": {"path": str(git_repo.path), "ref": ref}}) if git_repo else None
    w.store.ctx = SimpleNamespace(decisions=adapter, git=git)
    w.batch_ref = _batch_file(tmp_path)
    w.store.batch_roots = [str(tmp_path / "plan")]
    return w


# ------------------------------------------------------------- 1. Token parsing
def test_token_parsing_key_at_sha7_and_sha40(tmp_path: Path):
    sha7 = "1234567"
    adp7 = _decisions_adapter(tmp_path / "t7", f"Closes: demo:A@{sha7}")
    assert adp7.closes(DID) == {"demo:A"}
    assert adp7.closes_with_commits(DID) == {"demo:A": sha7}

    sha40 = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    adp40 = _decisions_adapter(tmp_path / "t40", f"Closes: demo:A@{sha40}")
    assert adp40.closes(DID) == {"demo:A"}
    assert adp40.closes_with_commits(DID) == {"demo:A": sha40}


def test_token_parsing_no_at_gives_none(tmp_path: Path):
    adp = _decisions_adapter(tmp_path, "Closes: demo:A")
    assert adp.closes(DID) == {"demo:A"}
    assert adp.closes_with_commits(DID) == {"demo:A": None}


def test_token_parsing_bad_sha_invalid(tmp_path: Path):
    for bad in ("demo:A@123456", f"demo:A@{'a' * 41}", "demo:A@123456g", "demo:A@",
                "demo:A@@1234567", "@1234567"):
        adp = _decisions_adapter(tmp_path / str(abs(hash(bad))), f"Closes: {bad}")
        assert adp.closes(DID) == set()
        assert adp.closes_with_commits(DID) == {}
        assert adp.has_closes_keyword(DID) is True


def test_token_parsing_mixed_tokens(tmp_path: Path):
    sha40 = "f" * 40
    cell = f"Closes: demo:A@1234567, demo:B, demo:C@badsha, demo:D@{sha40}"
    adp = _decisions_adapter(tmp_path, cell)
    assert adp.closes(DID) == {"demo:A", "demo:B", "demo:D"}
    assert adp.closes_with_commits(DID) == {
        "demo:A": "1234567",
        "demo:B": None,
        "demo:D": sha40,
    }


def test_bad_sha_fails_closed_on_close(tmp_path: Path):
    w = _world(tmp_path, cell="Closes: demo:A@badsha", kinds=("decision",))
    to_claimed(w, KEY)
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert "no valid project:ID token" in exc.value.msg
    w.close()


# ------------------------------------------------------------- 2. Config & ref validation
def test_config_both_shapes(tmp_path: Path):
    cfg_str = _make_config(tmp_path / "c1", {"repos": {"demo": "/srv/demo"}})
    assert cfg_str.repos["demo"] == "/srv/demo"
    assert GitAdapter(cfg_str.repos).repo_path("demo") == "/srv/demo"
    assert GitAdapter(cfg_str.repos).repo_ref("demo") == "main"

    cfg_dict = _make_config(tmp_path / "c2", {
        "repos": {"demo": {"path": "/srv/demo", "ref": "origin/main"}}
    })
    assert cfg_dict.repos["demo"] == {"path": "/srv/demo", "ref": "origin/main"}
    assert GitAdapter(cfg_dict.repos).repo_ref("demo") == "origin/main"

    cfg_def = _make_config(tmp_path / "c3", {"repos": {"demo": {"path": "/srv/demo"}}})
    assert cfg_def.repos["demo"] == {"path": "/srv/demo", "ref": "main"}
    assert GitAdapter(cfg_def.repos).repo_ref("demo") == "main"


def test_ref_validation_refuses_invalid(tmp_path: Path):
    for ok in ("main", "origin/main", "refs/heads/v1.0", "release_2026.01"):
        assert valid_ref(ok) is True
    for bad in ("-x", "--branch", "a..b", "feature branch", "", None, "bad@ref"):
        assert valid_ref(bad) is False

    for bad in ("-x", "a..b", "bad ref", ""):
        with pytest.raises(config.ConfigError):
            _make_config(tmp_path / f"b_{abs(hash(bad))}",
                         {"repos": {"demo": {"path": "/srv/demo", "ref": bad}}})


def test_git_adapter_is_ancestor(git_repo):
    adp = GitAdapter({"demo": str(git_repo.path)})
    assert adp.is_ancestor(str(git_repo.path), git_repo.main_sha, "main") is True
    assert adp.is_ancestor(str(git_repo.path), git_repo.feature_sha, "main") is False
    assert adp.is_ancestor_of_main(str(git_repo.path), git_repo.main_sha) is True
    assert adp.is_ancestor_of_main(str(git_repo.path), git_repo.feature_sha) is False
    assert adp.is_ancestor(str(git_repo.path), git_repo.main_sha, "-x") is False
    assert adp.is_ancestor(str(git_repo.path), git_repo.main_sha, "a..b") is False
    assert adp.is_ancestor(str(git_repo.path), git_repo.main_sha, "foo bar") is False


# ------------------------------------------------------------- 3. Tier none & edits
def test_tier_none_allowed_kinds_and_router_edits(tmp_path: Path, git_repo):
    w = _world(tmp_path, git_repo, kinds=("decision", "commit"))
    # Order-insensitive kinds allowed
    w.store.create_items(w.p("router"), [
        defn("A2", [], evidence_kinds=["commit", "decision"], review_tier="none"),
        defn("GOAL2", ["A2"], kind="goal"),
    ])
    for bad in (["commit"], ["decision", "ci"], ["any"]):
        with pytest.raises(Refused) as exc:
            w.store.create_items(w.p("router"), [
                defn("B", [], evidence_kinds=bad, review_tier="none"),
                defn("GOAL_B", ["B"], kind="goal"),
            ])
        assert exc.value.code == "validation"

    # Router edit on claimed item
    to_claimed(w, KEY)
    w.store.edit_definition(w.p("router"), KEY, {"evidence_kinds": ["decision", "commit"]})

    # Router edit on in_review item (moved from open via set_legacy_state)
    w.store.set_legacy_state(w.p("router"), "demo:A2", "in_review")
    w.store.edit_definition(w.p("router"), "demo:A2", {"evidence_kinds": ["commit", "decision"]})
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), "demo:A2", {"evidence_kinds": ["decision"]})
    assert exc.value.code == "forbidden"

    # add_evidence_router kind="commit" works on in_review
    unver = w.store.add_evidence_router(w.p("router"), "demo:A2", "commit", {"sha": git_repo.feature_sha})
    assert unver["verified"] is False
    ver = w.store.add_evidence_router(w.p("router"), "demo:A2", "commit", {"sha": git_repo.main_sha})
    assert ver["verified"] is True
    w.close()


# ------------------------------------------------------------- 4. Closes scenarios
def test_tier_none_decision_commit_no_commit_evidence_refused(tmp_path: Path, git_repo):
    w = _world(tmp_path, git_repo, kinds=("decision", "commit"))
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})

    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert "no commit evidence" in exc.value.msg
    w.close()


def test_tier_none_decision_commit_unmerged_refused(tmp_path: Path, git_repo):
    w = _world(tmp_path, git_repo, kinds=("decision", "commit"))
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert f"commit {git_repo.feature_sha} not merged into main" in exc.value.msg
    w.close()


def test_tier_none_decision_commit_merged_succeeds(tmp_path: Path, git_repo):
    w = _world(tmp_path, git_repo, kinds=("decision", "commit"),
               cell=f"Closes: {KEY}@{git_repo.feature_sha[:7]}")
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    # Merge feature into main
    _git_cmd(git_repo.path, "merge", "--ff-only", git_repo.feature_sha)

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert res["decision_ids"] == [DID]

    # Locked
    assert w.store._item(KEY)["state"] == "done"
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "locked"

    # Commit evidence row present
    commits = [r for r in w.store._evidence(KEY) if r["kind"] == "commit"]
    assert len(commits) == 1 and commits[0]["ref"]["sha"] == git_repo.feature_sha

    # fold and verify_integrity agree
    assert w.store.verify_integrity() == []
    folded = keeper_events.fold(keeper_events.read_events(w.conn))[KEY]
    assert folded["state"] == "done" and folded["reviewer"] == "router"
    w.close()


def test_decision_only_item_closes_as_before(tmp_path: Path):
    w = _world(tmp_path, kinds=("decision",))
    to_claimed(w, KEY)
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert w.store.verify_integrity() == []
    assert keeper_events.fold(keeper_events.read_events(w.conn))[KEY]["state"] == "done"
    w.close()


def test_configured_target_ref_per_repo(tmp_path: Path, git_repo):
    _git_cmd(git_repo.path, "branch", "release/1.0", git_repo.main_sha)
    w = _world(tmp_path, git_repo, kinds=("decision", "commit"), ref="release/1.0")
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    _git_cmd(git_repo.path, "merge", "--ff-only", git_repo.feature_sha)
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert f"commit {git_repo.feature_sha} not merged into release/1.0" in exc.value.msg

    _git_cmd(git_repo.path, "checkout", "-q", "release/1.0")
    _git_cmd(git_repo.path, "merge", "-q", "--ff-only", git_repo.feature_sha)
    _git_cmd(git_repo.path, "checkout", "-q", "main")

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert w.store.verify_integrity() == []
    assert keeper_events.fold(keeper_events.read_events(w.conn))[KEY]["state"] == "done"
    w.close()
