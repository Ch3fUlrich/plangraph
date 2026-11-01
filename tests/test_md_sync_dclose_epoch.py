"""KEEPER-DCLOSE: md-sync re-attaches D-row evidence at the item's current epoch (keeper close ignores older rows)."""
from test_md_sync_dclose import (build, commit_ref, dec_row, decision_refs, decs, item,  # noqa: F401
                                 repo)


def test_old_epoch_evidence_is_re_attached_before_the_close(repo, tmp_path):
    # KEEPER-DCLOSE: keeper close counts only current-epoch rows, so rows from an older epoch must be re-added
    text, view = decs(tmp_path, dec_row("D-505", "ship", f"demo:CODE-1@{repo.merged[:8]}"))
    old = [{"kind": "decision", "ref": {"id": "D-505"}, "epoch": 2},
           {"kind": "commit", "ref": {"sha": repo.merged}, "epoch": 2}]
    it = dict(item("CODE-1", kinds=("decision", "commit"), tier="none", evidence=old), epoch=4)
    p = build([it], text, view, repo)
    assert decision_refs(p.ops) == ["D-505"] and commit_ref(p.ops)["ref"]["sha"] == repo.merged
    it["evidence"] = [dict(e, epoch=4) for e in old]
    assert [o["op"] for o in build([it], text, view, repo).ops if o["op"] == "add_evidence"] == []
