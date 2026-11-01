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
from git_identity import identity_env

from keeper_util import World, defn
from plangraph.keeper import evidence
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter, LegacyCommitVerifier, resolve_commit
from plangraph.keeper.evidence_types import Result

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
    return f"{path}@{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _git_cmd(repo_path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, check=True,
        env=env or identity_env("Operator", "operator@example.invalid"),
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path):
    """Temp git repo with a commit on 'feature' merged into 'main'."""
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
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
           ref="main", cell=f"Closes: {KEY}", adapter=None) -> World:
    w = World(tmp_path)
    w.store.verifiers = dict(evidence.VERIFIERS)
    w.store.create_items(w.p("router"), [
        defn("A", [], evidence_kinds=list(kinds), review_tier="none"),
        defn("GOAL", ["A"], kind="goal"),
    ])
    if adapter is None:
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
    assert len(fabricated_token) == 40 and fabricated_token != feat_sha

    class _StubCommitVerifier:
        """Test-local stub accepting only injected abbreviated ref for testing commit evidence; lives only in this test."""
        kind = "commit"
        def verify(self, item, ref, ctx):
            s = (ref or {}).get("sha") or (ref or {}).get("commit")
            return Result(s == abbrev_sha, "injected" if s == abbrev_sha else "unverified", ref or {})

    w = _world(tmp_path, git_repo, cell=f"Closes: {KEY}@{fabricated_token}")
    w.store.verifiers["commit"] = _StubCommitVerifier()
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

    class _StubCommitVerifier:
        """Test-local stub accepting only injected abbreviated ref for testing commit evidence; lives only in this test."""
        kind = "commit"
        def verify(self, item, ref, ctx):
            s = (ref or {}).get("sha") or (ref or {}).get("commit")
            return Result(s == abbrev_sha, "injected" if s == abbrev_sha else "unverified", ref or {})

    w = _world(tmp_path, git_repo, cell=f"Closes: {KEY}@{feat_sha}")
    w.store.verifiers["commit"] = _StubCommitVerifier()
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": abbrev_sha})

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done" and res["decision_ids"] == [DID]
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
    """(g) same-row conflicts refuse; cross-row follows the SUPERSEDE RULE (later D wins)."""
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

    # g2: Cross-row mismatch -> SUPERSEDE: D-102 wins, so the close is refused
    # because the attached commit evidence (feature_sha) does not match 2222222.
    d_path = tmp_path / "g2" / "DECISIONS.md"
    d_path.parent.mkdir(parents=True, exist_ok=True)
    d_path.write_text(
        HEADER
        + f"| D-101 | demo | Closes: {KEY}@1111111 | router | ok |\n"
        + f"| D-102 | demo | Closes: {KEY}@2222222 | router | ok |\n",
        encoding="utf-8",
    )
    adapter = DecisionsAdapter(d_path)
    w2 = _world(tmp_path / "g2", git_repo, adapter=adapter)

    w2.store.set_legacy_state(w2.p("router"), KEY, "in_review")
    w2.store.add_evidence_router(w2.p("router"), KEY, "decision", {"id": "D-101"})
    w2.store.add_evidence_router(w2.p("router"), KEY, "decision", {"id": "D-102"})
    w2.store.add_evidence_router(w2.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    with pytest.raises(Refused) as exc2:
        w2.store.close_on_decision(w2.p("router"), KEY, w2.batch_ref)
    assert exc2.value.code == "evidence_unverified"
    assert f"Closes token {KEY}@2222222 does not match" in exc2.value.msg
    assert git_repo.feature_sha in exc2.value.msg
    w2.close()

    # g3: Cross-row supersede where the later row names the attached commit
    # evidence -> the close SUCCEEDS with the later sha.
    g3_path = tmp_path / "g3" / "DECISIONS.md"
    g3_path.parent.mkdir(parents=True, exist_ok=True)
    g3_path.write_text(
        HEADER
        + f"| D-101 | demo | Closes: {KEY}@1111111 | router | ok |\n"
        + f"| D-102 | demo | Closes: {KEY}@{git_repo.feature_sha} | router | ok |\n",
        encoding="utf-8",
    )
    g3_adapter = DecisionsAdapter(g3_path)
    w3 = _world(tmp_path / "g3", git_repo, adapter=g3_adapter)
    w3.store.set_legacy_state(w3.p("router"), KEY, "in_review")
    w3.store.add_evidence_router(w3.p("router"), KEY, "decision", {"id": "D-101"})
    w3.store.add_evidence_router(w3.p("router"), KEY, "decision", {"id": "D-102"})
    w3.store.add_evidence_router(w3.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})
    res3 = w3.store.close_on_decision(w3.p("router"), KEY, w3.batch_ref)
    assert res3["state"] == "done"
    assert res3["decision_ids"] == ["D-101", "D-102"]
    w3.close()


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
    w = _world(tmp_path / "multi_pfx", git_repo, adapter=adapter)

    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-101"})
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-102"})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})

    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert res["decision_ids"] == ["D-101", "D-102"]
    w.close()


def test_supersede_three_rows_last_wins(tmp_path: Path, git_repo):
    """SUPERSEDE: D-101@x, D-102@y, D-103@z -> z governs (later D wins)."""
    d_path = tmp_path / "three" / "DECISIONS.md"
    d_path.parent.mkdir(parents=True, exist_ok=True)
    d_path.write_text(
        HEADER
        + f"| D-101 | demo | Closes: {KEY}@1111111 | router | ok |\n"
        + f"| D-102 | demo | Closes: {KEY}@2222222 | router | ok |\n"
        + f"| D-103 | demo | Closes: {KEY}@{git_repo.feature_sha} | router | ok |\n",
        encoding="utf-8",
    )
    adapter = DecisionsAdapter(d_path)
    w = _world(tmp_path / "three", git_repo, adapter=adapter)
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-101"})
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-102"})
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-103"})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})
    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    assert res["decision_ids"] == ["D-101", "D-102", "D-103"]
    w.close()


def test_supersede_prefix_compatible_rows_close(tmp_path: Path, git_repo):
    """Prefix-compatible rows keep today's behaviour (longer wins) under SUPERSEDE."""
    d_path = tmp_path / "pfx2" / "DECISIONS.md"
    d_path.parent.mkdir(parents=True, exist_ok=True)
    d_path.write_text(
        HEADER
        + f"| D-101 | demo | Closes: {KEY}@{git_repo.feature_sha} | router | ok |\n"
        + f"| D-102 | demo | Closes: {KEY}@{git_repo.feature_sha[:7]} | router | ok |\n",
        encoding="utf-8",
    )
    adapter = DecisionsAdapter(d_path)
    w = _world(tmp_path / "pfx2", git_repo, adapter=adapter)
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-101"})
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-102"})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": git_repo.feature_sha})
    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done"
    w.close()


def test_commit_exists_and_verifier_refuse_abbrev_and_uppercase(git_repo):
    """(i) abbreviated sha (7 hex) and uppercase full sha are refused; valid lowercase full sha verifies."""
    valid_sha, repo_dir = git_repo.feature_sha, str(git_repo.path)
    abbrev_sha, upper_sha = valid_sha[:7], valid_sha.upper()
    adapter = GitAdapter({"demo": {"path": repo_dir, "ref": "main"}})
    verifier = LegacyCommitVerifier()
    ctx = SimpleNamespace(git=adapter)
    item = {"project": "demo"}

    assert adapter.commit_exists(repo_dir, abbrev_sha) is False
    assert adapter.commit_exists(repo_dir, upper_sha) is False
    assert adapter.commit_exists(repo_dir, valid_sha) is True

    expected_refusal = "commit requires a full lowercase commit sha (40 or 64 hex)"
    res_abbrev = verifier.verify(item, {"sha": abbrev_sha}, ctx)
    assert not res_abbrev.ok and expected_refusal in res_abbrev.detail

    res_upper = verifier.verify(item, {"sha": upper_sha}, ctx)
    assert not res_upper.ok and expected_refusal in res_upper.detail

    res_valid = verifier.verify(item, {"sha": valid_sha}, ctx)
    assert res_valid.ok and f"commit {valid_sha} is an ancestor of main" in res_valid.detail


def test_decision_only_item_with_sha_token_closes(tmp_path: Path):
    """(ii) decision-only item with no commit kind closes when decision row names one @sha token."""
    w = _world(tmp_path, kinds=["decision"], cell=f"Closes: {KEY}@abcdef0123")
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done" and res["decision_ids"] == [DID]
    w.close()


def test_resolve_commit_skipped_mutation_fails(tmp_path: Path, git_repo):
    """(iii) close fails if resolve_commit is skipped (comparing token with raw evidence sha instead of resolved id)."""
    feat_sha = git_repo.feature_sha
    abbrev_sha = feat_sha[:7]

    class _StubCommitVerifier:
        """Test-local stub accepting only injected abbreviated ref for testing commit evidence; lives only in this test."""
        kind = "commit"
        def verify(self, item, ref, ctx):
            s = (ref or {}).get("sha") or (ref or {}).get("commit")
            return Result(s == abbrev_sha, "injected" if s == abbrev_sha else "unverified", ref or {})

    w = _world(tmp_path, git_repo, cell=f"Closes: {KEY}@{feat_sha[:10]}")
    w.store.verifiers["commit"] = _StubCommitVerifier()
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": abbrev_sha})
    res = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert res["state"] == "done" and res["decision_ids"] == [DID]
    w.close()
