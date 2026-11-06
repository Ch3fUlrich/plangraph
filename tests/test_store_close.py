"""Target-ref naming in the store_close commit-evidence refusal (PR #309 r3).

``_check_commit_evidence`` must name the git adapter's required ``repo_ref``
target in its ``commit ... not merged into <ref>`` refusal, never an invented
``main``; only the absent-adapter (``git is None``) case keeps its None check.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_fakes import FakeGit
from keeper_util import World, defn
from plangraph.keeper import evidence
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

DID = "D-505"
KEY = "demo:A"
SHA = "a" * 40
HEADER = "| id | project | decision | by | why / evidence |\n|---|---|---|---|---|\n"


class TargetRefGit(FakeGit):
    """FakeGit whose required ``repo_ref`` names a non-default target ref."""

    def __init__(self, target_ref):
        super().__init__(ancestor=False, trailers={"Plan-Item": KEY})
        self.target_ref = target_ref
        self.repo_ref_calls = []

    def repo_ref(self, project=None, *args, **kwargs):
        self.repo_ref_calls.append(project)
        return self.target_ref


def _world(tmp_path: Path, git):
    """Tier-'none' item with decision+commit evidence ready to close."""
    w = World(tmp_path)
    w.store.verifiers = dict(evidence.VERIFIERS)
    w.store.create_items(w.p("router"), [
        defn("A", [], evidence_kinds=["decision", "commit"], review_tier="none"),
        defn("GOAL", ["A"], kind="goal"),
    ])
    decisions = tmp_path / "DECISIONS.md"
    decisions.write_text(
        HEADER + f"| {DID} | demo | Closes: {KEY} | router | tested |\n",
        encoding="utf-8")
    root = tmp_path / "plan"
    root.mkdir(parents=True, exist_ok=True)
    batch = root / "batch.md"
    batch.write_text("batch content\n", encoding="utf-8")
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(decisions), git=git)
    w.store.batch_roots = [str(root)]
    w.batch_ref = f"{batch}@{hashlib.sha256(batch.read_bytes()).hexdigest()}"
    w.store.set_legacy_state(w.p("router"), KEY, "in_review")
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": SHA})
    return w


def test_refusal_names_the_adapters_repo_ref_never_invented_main(tmp_path: Path):
    git = TargetRefGit("release/1.0")
    w = _world(tmp_path, git)
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert f"commit {SHA} not merged into release/1.0" in exc.value.msg
    assert "not merged into main" not in exc.value.msg
    assert git.repo_ref_calls  # the named target ref came from the adapter
    w.close()


def test_absent_git_adapter_keeps_the_none_check_refusal(tmp_path: Path):
    w = _world(tmp_path, None)
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert f"commit {SHA} not merged into main" in exc.value.msg
    w.close()
