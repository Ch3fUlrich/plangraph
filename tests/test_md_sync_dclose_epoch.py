"""KEEPER-DCLOSE end-to-end: D-row evidence re-attached at the item's current epoch.

The keeper counts evidence rows only at the item's current epoch (store_close):
the real store -- claim, attach decision + commit evidence, submit, reopen --
must show the md-sync plan re-attaching the stale rows and the close passing
only after they land.
"""
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import apply_batch as ab  # noqa: E402
import md_sync_dclose as dclose  # noqa: E402
import md_sync_plan as plan  # noqa: E402
from keeper_util import World, defn, to_claimed  # noqa: E402
from plangraph.keeper.errors import Refused  # noqa: E402
from plangraph.keeper.evidence_git import GitAdapter, LegacyCommitVerifier  # noqa: E402
from test_md_sync_dclose import dec_row, decs, repo, repos  # noqa: F401, E402

KEY = "demo:CODE-1"


def reopened_world(repo, tmp_path):
    """Real store: claim, attach decision + commit evidence, submit, reopen."""
    w = World(tmp_path)
    w.store.create_items(w.p("router"), [
        defn("CODE-1", evidence_kinds=("decision", "commit"), review_tier="none"),
        defn("GOAL", ["CODE-1"], kind="goal")])
    w.store.verifiers["commit"] = LegacyCommitVerifier()
    text, view = decs(tmp_path, dec_row("D-505", "ship", f"{KEY}@{repo.merged[:8]}"))
    w.store.ctx = SimpleNamespace(decisions=view, git=GitAdapter(repos(repo)))
    root = tmp_path / "plan"
    root.mkdir()
    (root / "batch.md").write_text("batch body\n", encoding="utf-8")
    w.store.batch_roots = [str(root)]
    epoch = to_claimed(w, KEY)
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-505"})
    w.store.add_evidence_router(w.p("router"), KEY, "commit", {"sha": repo.merged})
    w.store.submit(w.p("l1main"), KEY, epoch)
    w.store.reopen(w.p("router"), KEY, "redo the close", "D-505")
    return w, text, view


def planned(w, text, view, repo):
    """The md-sync plan over the store's real snapshot items."""
    items = w.store.snapshot(w.p("router"))["items"]
    return plan.build_plan([], [], [], set(), items, view,
                           decisions_text=text, repos=repos(repo)).ops


def run_ops(w, ops, batch):
    """Apply the way md-sync apply does: apply_batch.build_request -> keeper verb."""
    digest = hashlib.sha256(batch.read_bytes()).hexdigest()
    for op in ops:
        verb, params = ab.build_request(op, str(batch), digest)
        getattr(w.store, verb)(w.p("router"), **params)


def test_re_attached_evidence_unblocks_the_close(repo, tmp_path):
    w, text, view = reopened_world(repo, tmp_path)
    ops = planned(w, text, view, repo)
    assert {o["kind"] for o in ops if o["op"] == "add_evidence"} == {"decision", "commit"}
    assert ops[-1]["op"] == "close_on_decision" and ops[-1]["key"] == KEY
    batch = tmp_path / "plan" / "batch.md"
    run_ops(w, [o for o in ops if o["op"] == "add_evidence"], batch)
    # idempotence from the real post-apply snapshot: the re-attached rows now
    # count at the current epoch, so no add_evidence op is planned again
    assert [o for o in planned(w, text, view, repo) if o["op"] == "add_evidence"] == []
    run_ops(w, [o for o in ops if o["op"] == "close_on_decision"], batch)
    item = {i["key"]: i for i in w.store.snapshot(w.p("router"))["items"]}[KEY]
    assert item["state"] == "done"
    assert [o for o in planned(w, text, view, repo) if o["op"] == "add_evidence"] == []
    assert w.store.verify_integrity() == []
    w.close()


def test_the_close_is_refused_before_the_re_attach_ops(repo, tmp_path):
    w, _, _ = reopened_world(repo, tmp_path)
    batch = tmp_path / "plan" / "batch.md"
    batch_ref = f"{batch}@{hashlib.sha256(batch.read_bytes()).hexdigest()}"
    # called directly before any re-attach op: the older-epoch rows never count
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, batch_ref)
    assert exc.value.code == "evidence_missing"
    # the incident's shape: decision re-attached, commit evidence still at the
    # older epoch -- refused evidence_unverified until the commit evidence lands
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": "D-505"})
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert "no commit evidence" in exc.value.msg
    w.close()


def test_a_row_without_an_epoch_never_counts_at_an_epoched_item():
    # the fail-closed filter contract: an unknown epoch is not a match
    assert dclose._current({"epoch": 2}, {"kind": "decision"}) is False
    assert dclose._current({"epoch": 2}, {"epoch": 2, "kind": "decision"}) is True
    assert dclose._current({}, {"kind": "decision"}) is True  # legacy fixtures
