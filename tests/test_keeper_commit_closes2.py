"""Tests for commit-gated decision closes (D-505 fix2).

1. Resolution of commit evidence shas to full 40-hex commit ids and prefix matching.
2. Refusal of conflicting Closes: tokens in DecisionsAdapter and store_close.py.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from keeper_util import World, defn
from plangraph.keeper import evidence
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter, resolve_commit

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
    """Temp git repo with a commit on 'feature' merged into 'main'."""
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
    _git_cmd(repo, "merge", "--ff-only", feature_sha)
    return SimpleNamespace(path=repo, main_sha=main_sha, feature_sha=feature_sha)


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


def test_abbrev_evidence_fabricated_40hex_token_refused(tmp_path: Path, git_repo):
    """(a) abbreviated 7-hex evidence of a merged commit + 40-hex token = same 7 hex + 33 other hex -> Refused."""
    feat_sha = git_repo.feature_sha
    abbrev_sha = feat_sha[:7]
    fabricated_token = abbrev_sha + "0" * 33
    assert len(fabricated_token) == 40
    assert fabricated_token != feat_sha

    w = _world(tmp_path, git_repo, cell=f"Closes: {KEY}@{fabricated_token}")
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": abbrev_sha})

    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert f"Closes token {KEY}@{fabricated_token} does not match the verified commit evidence ['{feat_sha}']" in exc.value.msg
    w.close()


def test_abbrev_evidence_full_token_closes(tmp_path: Path, git_repo):
    """(b) abbreviated evidence + the real full id as token -> closes."""
    feat_sha = git_repo.feature_sha
    abbrev_sha = feat_sha[:7]

    w = _world(tmp_path, git_repo, cell=f"Closes: {KEY}@{feat_sha}")
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": abbrev_sha})

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert res["decision_ids"] == [DID]
    w.close()


def test_full_evidence_abbrev_token_closes(tmp_path: Path, git_repo):
    """(c) full evidence + 7-hex prefix token -> closes."""
    feat_sha = git_repo.feature_sha
    abbrev_tok = feat_sha[:7]

    w = _world(tmp_path, git_repo, cell=f"Closes: {KEY}@{abbrev_tok}")
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": feat_sha})

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert res["decision_ids"] == [DID]
    w.close()


def test_token_less_than_7_hex_not_valid(tmp_path: Path, git_repo):
    """(d) token < 7 hex -> not a valid token (existing behaviour)."""
    cell = f"Closes: {KEY}@123456"
    adp = _decisions_adapter(tmp_path / "d_adp", cell)
    assert adp.closes_with_commits(DID) == {}
    assert adp.closes(DID) == set()
    assert adp.has_closes_keyword(DID) is True

    w = _world(tmp_path, git_repo, cell=cell)
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert "no valid project:ID token" in exc.value.msg
    w.close()


def test_resolve_commit_cases(tmp_path: Path):
    """(e) resolve_commit: valid full, valid abbreviated, unknown sha, '-x', garbage, a tag/blob sha -> None."""
    repo = tmp_path / "repo_e"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git_cmd(repo, "config", "user.email", "operator@example.invalid")
    _git_cmd(repo, "config", "user.name", "Operator")
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    _git_cmd(repo, "add", "f.txt")
    _git_cmd(repo, "commit", "-q", "-m", "commit 1")
    full_sha = _git_cmd(repo, "rev-parse", "HEAD")
    abbrev_sha = full_sha[:7]

    # Create a blob and a tag pointing to that blob (not a commit)
    blob_sha = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="blob content", text=True, capture_output=True, check=True,
    ).stdout.strip()
    _git_cmd(repo, "tag", "blob-tag", blob_sha)

    adp = GitAdapter({"demo": str(repo)})

    # Valid full
    assert resolve_commit(repo, full_sha) == full_sha
    assert adp.resolve_commit(str(repo), full_sha) == full_sha

    # Valid abbreviated
    assert resolve_commit(repo, abbrev_sha) == full_sha
    assert adp.resolve_commit(str(repo), abbrev_sha) == full_sha

    # Unknown sha
    unknown = "0" * 40
    assert resolve_commit(repo, unknown) is None
    assert adp.resolve_commit(str(repo), unknown) is None

    # Leading '-'
    assert resolve_commit(repo, "-x") is None
    assert resolve_commit(repo, "--end-of-options") is None
    assert adp.resolve_commit(str(repo), "-x") is None

    # Garbage
    assert resolve_commit(repo, "not_a_valid_sha!") is None
    assert resolve_commit(repo, "zzz1234") is None
    assert adp.resolve_commit(str(repo), "garbage") is None

    # Blob sha (not a commit)
    assert resolve_commit(repo, blob_sha) is None
    assert adp.resolve_commit(str(repo), blob_sha) is None

    # Tag sha of blob (tag dereferencing to non-commit)
    tag_sha = _git_cmd(repo, "rev-parse", "blob-tag")
    assert resolve_commit(repo, tag_sha) is None
    assert adp.resolve_commit(str(repo), tag_sha) is None


def test_closes_with_commits_variations(tmp_path: Path):
    """(f) closes_with_commits: two different shas same key -> key absent; same sha twice -> present; prefix pair -> the longer."""
    # Two different shas where neither is a prefix -> key absent
    cell_diff = "Closes: demo:A@aaaaaaa, demo:A@bbbbbbb"
    adp_diff = _decisions_adapter(tmp_path / "f_diff", cell_diff)
    assert "demo:A" not in adp_diff.closes_with_commits(DID)
    assert adp_diff.closes(DID) == set()
    assert adp_diff.has_closes_keyword(DID) is True

    # Same sha twice -> present
    cell_same = "Closes: demo:A@aaaaaaa, demo:A@aaaaaaa"
    adp_same = _decisions_adapter(tmp_path / "f_same", cell_same)
    assert adp_same.closes_with_commits(DID) == {"demo:A": "aaaaaaa"}
    assert adp_same.closes(DID) == {"demo:A"}

    # Prefix pair -> keeps the longer
    cell_pfx1 = "Closes: demo:A@aaaaaaa, demo:A@aaaaaaa1234"
    adp_pfx1 = _decisions_adapter(tmp_path / "f_pfx1", cell_pfx1)
    assert adp_pfx1.closes_with_commits(DID) == {"demo:A": "aaaaaaa1234"}
    assert adp_pfx1.closes(DID) == {"demo:A"}

    # Prefix pair reverse order -> keeps the longer
    cell_pfx2 = "Closes: demo:A@aaaaaaa1234, demo:A@aaaaaaa"
    adp_pfx2 = _decisions_adapter(tmp_path / "f_pfx2", cell_pfx2)
    assert adp_pfx2.closes_with_commits(DID) == {"demo:A": "aaaaaaa1234"}
    assert adp_pfx2.closes(DID) == {"demo:A"}

    # Key plus key without @sha -> present with sha
    cell_none1 = "Closes: demo:A, demo:A@aaaaaaa"
    adp_none1 = _decisions_adapter(tmp_path / "f_none1", cell_none1)
    assert adp_none1.closes_with_commits(DID) == {"demo:A": "aaaaaaa"}

    cell_none2 = "Closes: demo:A@aaaaaaa, demo:A"
    adp_none2 = _decisions_adapter(tmp_path / "f_none2", cell_none2)
    assert adp_none2.closes_with_commits(DID) == {"demo:A": "aaaaaaa"}


def test_close_with_conflicting_tokens_refused(tmp_path: Path, git_repo):
    """(g) a close with conflicting tokens is refused."""
    # g1: Single decision with conflicting shas -> key invalid/absent -> close refused
    cell_single = "Closes: demo:A@1111111, demo:A@2222222"
    w1 = _world(tmp_path / "g1", git_repo, cell=cell_single)
    w1.store.set_legacy_state(w1.p("router"), KEY, "in_review")
    w1.store.add_evidence_router(w1.p("router"), KEY, "decision", {"id": DID})
    w1.store.add_evidence_router(w1.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})
    with pytest.raises(Refused) as exc1:
        w1.store.close_on_decision(w1.p("router"), KEY, w1.batch_ref)
    assert exc1.value.code == "evidence_unverified"
    w1.close()

    # g2: Multiple decision rows attached with conflicting tokens -> _closes_token_sha raises
    d_path = tmp_path / "g2" / "DECISIONS.md"
    d_path.parent.mkdir(parents=True, exist_ok=True)
    d_path.write_text(
        HEADER
        + f"| D-101 | demo | Closes: {KEY}@1111111 | router | ok |\n"
        + f"| D-102 | demo | Closes: {KEY}@2222222 | router | ok |\n",
        encoding="utf-8",
    )
    adapter = DecisionsAdapter(d_path)
    w2 = World(tmp_path / "g2")
    w2.store.verifiers = dict(evidence.VERIFIERS)
    w2.store.create_items(w2.p("router"), [
        defn("A", [], evidence_kinds=["decision", "commit"], review_tier="none"),
        defn("GOAL", ["A"], kind="goal"),
    ])
    git = GitAdapter({"demo": {"path": str(git_repo.path), "ref": "main"}})
    w2.store.ctx = SimpleNamespace(decisions=adapter, git=git)
    w2.batch_ref = _batch_file(tmp_path / "g2")
    w2.store.batch_roots = [str(tmp_path / "g2" / "plan")]

    w2.store.set_legacy_state(w2.p("router"), KEY, "in_review")
    w2.store.add_evidence_router(w2.p("router"), KEY, "decision", {"id": "D-101"})
    w2.store.add_evidence_router(w2.p("router"), KEY, "decision", {"id": "D-102"})
    w2.store.add_evidence_router(w2.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    with pytest.raises(Refused) as exc2:
        w2.store.close_on_decision(w2.p("router"), KEY, w2.batch_ref)
    assert exc2.value.code == "evidence_unverified"
    assert f"conflicting Closes tokens for {KEY}" in exc2.value.msg
    w2.close()


def test_missing_git_adapter_fail_closed(tmp_path: Path, git_repo):
    """Missing git adapter in keeper context fails closed."""
    w = _world(tmp_path, git_repo, cell=f"Closes: {KEY}@{git_repo.feature_sha}")
    w.store.ctx.git = None
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    w.close()


def test_multiple_decision_rows_prefix_pair_closes(tmp_path: Path, git_repo):
    """Multiple decision rows with prefix shas merge to the longer sha and close."""
    d_path = tmp_path / "multi_pfx" / "DECISIONS.md"
    d_path.parent.mkdir(parents=True, exist_ok=True)
    d_path.write_text(
        HEADER
        + f"| D-101 | demo | Closes: {KEY}@{git_repo.feature_sha[:7]} | router | ok |\n"
        + f"| D-102 | demo | Closes: {KEY}@{git_repo.feature_sha} | router | ok |\n",
        encoding="utf-8",
    )
    adapter = DecisionsAdapter(d_path)
    w = World(tmp_path / "multi_pfx")
    w.store.verifiers = dict(evidence.VERIFIERS)
    w.store.create_items(w.p("router"), [
        defn("A", [], evidence_kinds=["decision", "commit"], review_tier="none"),
        defn("GOAL", ["A"], kind="goal"),
    ])
    git = GitAdapter({"demo": {"path": str(git_repo.path), "ref": "main"}})
    w.store.ctx = SimpleNamespace(decisions=adapter, git=git)
    w.batch_ref = _batch_file(tmp_path / "multi_pfx")
    w.store.batch_roots = [str(tmp_path / "multi_pfx" / "plan")]

    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-101"})
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-102"})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert res["decision_ids"] == ["D-101", "D-102"]
    w.close()
