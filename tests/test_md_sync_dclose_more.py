"""md_sync D-505: router D-rows with ``Closes: key@sha`` -- additional tests."""
import hashlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import md_sync  # noqa: E402
import md_sync_dclose as dclose  # noqa: E402
import md_sync_parse as mp  # noqa: E402
import md_sync_plan as plan  # noqa: E402
from plangraph.keeper.evidence_git import GitAdapter, LegacyCommitVerifier  # noqa: E402

from test_md_sync_dclose import (  # noqa: E402
    SEG_A, SEG_B, assert_ops_valid, build, close_ops, dec_row, decs,
    decision_refs, item, repo, repos,
)


# ------------------------------------------- ANSWERS + D-row dedupe (one close)

def test_answers_and_drow_share_one_close_with_all_d_ids(repo, tmp_path):
    answers = [mp.Answer("CODE-1", ["D-515"], "2026-10-01", "done")]
    text, view = decs(tmp_path,
                      dec_row("D-505", "ship code one",
                              f"demo:CODE-1@{repo.merged[:8]}"),
                      dec_row("D-515", "operator follow-up", None, by="operator"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo,
              answers=answers, ids={"D-505", "D-515"})
    assert len(close_ops(p.ops)) == 1
    assert sorted(decision_refs(p.ops)) == ["D-505", "D-515"]
    assert p.d_closes == [{"key": "demo:CODE-1", "decision": "D-515"}]
    assert p.close_skipped == []


def test_answers_cited_row_without_the_key_is_dropped_and_reported(repo,
                                                                   tmp_path):
    answers = [mp.Answer("CODE-1", ["D-516"], "2026-10-01", "done")]
    text, view = decs(tmp_path,
                      dec_row("D-505", "ship code one",
                              f"demo:CODE-1@{repo.merged[:8]}"),
                      dec_row("D-516", "closes somebody else",
                              "demo:OTHER", by="operator"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo,
              answers=answers, ids={"D-505", "D-516"})
    assert p.close_skipped == [("CODE-1", "D-516", "demo:CODE-1")]
    assert len(close_ops(p.ops)) == 1
    assert decision_refs(p.ops) == ["D-505"]  # the conflicting D-516 was dropped


# --------------------------------------------------- Models trailer (D-547)

def test_models_trailer_copied_verbatim_on_a_merged_close(repo, tmp_path):
    row = (f"| D-505 | demo | ship code one Models: {SEG_A} "
           f"Closes: demo:CODE-1@{repo.merged[:8]} | router | why |\n")
    text, view = decs(tmp_path, row)
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert p.ops[0]["op"] == "edit"
    assert p.ops[0]["fields"]["note"] == f"D-505 {SEG_A}"
    assert p.ops[0]["fields"]["review_tier"] == "none"
    assert p.models == [{"key": "demo:CODE-1", "decision": "D-505",
                         "text": SEG_A}]
    assert len(close_ops(p.ops)) == 1


def test_models_trailer_on_an_in_review_item(repo, tmp_path):
    row = (f"| D-506 | demo | ship code two Models: {SEG_B} "
           f"Closes: demo:CODE-2@{repo.unmerged[:8]} | router | why |\n")
    text, view = decs(tmp_path, row)
    p = build([item("CODE-2", kinds=("commit",))], text, view, repo)
    assert [o["op"] for o in p.ops] == ["edit", "legacy_state"]
    assert p.ops[0]["fields"] == {"note": f"D-506 {SEG_B}"}
    assert p.models == [{"key": "demo:CODE-2", "decision": "D-506",
                         "text": SEG_B}]


def test_models_trailer_appends_to_an_existing_note(repo, tmp_path):
    row = (f"| D-505 | demo | ship code one Models: {SEG_A} "
           f"Closes: demo:CODE-1@{repo.merged[:8]} | router | why |\n")
    text, view = decs(tmp_path, row)
    p = build([item("CODE-1", kinds=("commit",), note="older note")],
              text, view, repo)
    assert p.ops[0]["fields"]["note"] == f"older note | D-505 {SEG_A}"


def test_absent_trailer_changes_nothing(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        f"demo:CODE-1@{repo.merged[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert all("note" not in (o.get("fields") or {}) for o in p.ops
               if o["op"] == "edit")
    assert p.models == []


def test_second_run_does_not_repeat_a_models_segment(repo, tmp_path):
    row = (f"| D-505 | demo | ship code one Models: {SEG_A} "
           f"Closes: demo:CODE-1@{repo.merged[:8]} | router | why |\n")
    text, view = decs(tmp_path, row)
    p = build([item("CODE-1", kinds=("commit",), note=f"D-505 {SEG_A}")],
              text, view, repo)
    # tier fields only: the segment already present is not repeated
    assert p.ops[0]["fields"] == {"review_tier": "none",
                                  "evidence_kinds": ["decision", "commit"]}
    assert p.models == []


def test_two_drows_one_key_gets_both_segments_each_once(repo, tmp_path):
    rows = (f"| D-505 | demo | one Models: {SEG_A} "
            f"Closes: demo:CF2@{repo.merged[:8]} | router | why |\n"
            f"| D-506 | demo | two Models: {SEG_B} "
            f"Closes: demo:CF2@{repo.merged[:10]} | router | why |\n")
    text, view = decs(tmp_path, rows)
    p = build([item("CF2", kinds=("commit",))], text, view, repo)
    assert p.ops[0]["fields"]["note"] == f"D-505 {SEG_A} | D-506 {SEG_B}"
    assert [(m["decision"], m["text"]) for m in p.models] == [
        ("D-505", SEG_A), ("D-506", SEG_B)]
    assert len(close_ops(p.ops)) == 1
    assert sorted(decision_refs(p.ops)) == ["D-505", "D-506"]


def test_hostile_segment_newlines_collapse_and_a_pipe_is_left_to_the_keeper_parser():
    assert dclose.sanitize_segment("a\n\tb") == "a b"
    assert dclose.sanitize_segment("") is None
    # a | ends the cell in the keeper parser, which drops the whole row before this module sees it
    assert dclose.sanitize_segment("a|b") == "a|b"
    cell = f"Models: a|b Closes: demo:Z@abcdef1"
    assert dclose.models_segment(cell) == "a|b"
    assert dclose.sanitize_segment(dclose.models_segment(cell)) == "a|b"
    assert dclose.models_segment("no trailer here") is None
    # a Models: trailer at the end of the cell takes everything after it
    cell2 = "prose Models: last words Closes: demo:Z@abcdef1"
    assert dclose.models_segment(cell2) == "last words"


def test_credential_shaped_segment_is_masked_before_it_leaves_md_sync(repo,
                                                                      tmp_path):
    # synthetic, not a credential
    hostile = "login see " + "add" + "/" + "commit" + "x99"
    row = (f"| D-505 | demo | one Models: {hostile} "
           f"Closes: demo:CODE-1@{repo.merged[:8]} | router | why |\n")
    text, view = decs(tmp_path, row)
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    note = p.ops[0]["fields"]["note"]
    assert "add/commitx99" not in note and "[redacted]" in note
    assert p.redactions >= 1


def test_models_note_over_the_keeper_cap_is_refused_with_a_warning(repo,
                                                                   tmp_path):
    long_seg = "x" * 2100
    row = (f"| D-505 | demo | one Models: {long_seg} "
           f"Closes: demo:CODE-1@{repo.merged[:8]} | router | why |\n")
    text, view = decs(tmp_path, row)
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert "note" not in p.ops[0]["fields"]
    assert p.warnings and "exceed" in p.warnings[0]["text"]
    assert len(close_ops(p.ops)) == 1  # the close still happens


def test_keeper_refuses_a_credential_shaped_note():
    # the refusal the mask exists for: the keeper's own note validation
    # synthetic, not a credential
    from plangraph.keeper.credential_guard import guard
    from plangraph.keeper.errors import Refused
    with pytest.raises(Refused):
        # synthetic, not a credential
        guard("note", "login see " + "add" + "/" + "commit" + "x99")


# ------------------------------------------------- report / status shape

def test_report_lines_summary_and_status_shape(repo, tmp_path):
    rows = (dec_row("D-505", "one Models: " + SEG_A,
                    f"demo:CODE-1@{repo.merged[:8]}")
            + dec_row("D-506", "two Models: " + SEG_B,
                      f"demo:CODE-2@{repo.unmerged[:8]}")
            + dec_row("D-508", "three", "demo:CODE-3")
            + dec_row("D-510", "decided", f"demo:DEC-1@{repo.merged[:8]}")
            + dec_row("D-512", "bad", "demo:BAD@zzz"))
    text, view = decs(tmp_path, rows)
    items = [item("CODE-1", kinds=("commit",)),
             item("CODE-2", kinds=("commit",)),
             item("CODE-3", kinds=("commit",)),
             item("DEC-1", tier="none", kinds=("decision",))]
    p = build(items, text, view, repo)
    out = io.StringIO()
    md_sync.report(p, out, dry=True)
    lines = out.getvalue().splitlines()
    for expected in ("close demo:CODE-1: by D-505",
                     f"in_review demo:CODE-2: D-506 @{repo.unmerged[:8]}"
                     " not on main",
                     "listed demo:CODE-3: D-508 no sha",
                     "close demo:DEC-1: by D-510",
                     "warning: sha ignored on decision-only item demo:DEC-1",
                     f"models demo:CODE-1: {SEG_A}",
                     f"models demo:CODE-2: {SEG_B}",
                     "invalid Closes token in D-512"):
        assert expected in lines, expected
    assert any("(no calls made)" in ln for ln in lines)
    summary = [ln for ln in lines if ln.startswith("md-sync: ")][-1]
    assert summary.endswith("close-skipped 0, d-closes 2, in-review 2, "
                            "warnings 1")
    assert_ops_valid(p.ops)

    status = md_sync.status_doc("dry")
    md_sync.fill_status(status, p)
    assert list(status) == ["ts", "mode", "ops", "applied", "skipped",
                            "refused", "gone", "redactions", "close_skipped",
                            "d_closes", "d_in_review", "warnings", "models", "unmatched", "error"]
    assert status["d_closes"] == [
        {"key": "demo:CODE-1", "decision": "D-505"},
        {"key": "demo:DEC-1", "decision": "D-510"}]
    assert status["d_in_review"] == [
        {"key": "demo:CODE-2", "decision": "D-506",
         "sha": repo.unmerged[:8], "reason": "not on main"},
        {"key": "demo:CODE-3", "decision": "D-508", "sha": None,
         "reason": "no sha"}]
    assert status["warnings"] == [
        {"key": "demo:DEC-1", "decision": "D-510",
         "text": "sha ignored on decision-only item demo:DEC-1"}]


# ------------------------------------------------ in-process apply (e2e)

@pytest.fixture
def world(repo, tmp_path):
    from keeper_util import World, seed
    w = World(tmp_path)
    keys = seed(w, [("CODE-1", []), ("CODE-2", [])], project="demo",
                evidence_kinds=("commit",))
    w.store.verifiers["commit"] = LegacyCommitVerifier()
    w.store.ctx = SimpleNamespace(
        decisions=None, git=GitAdapter(repos(repo)))
    w.store.batch_roots = [str(tmp_path)]
    return w


def live_items(world):
    return world.store.snapshot(world.p("router"))["items"]


def test_apply_closes_the_merged_code_item_and_leaves_the_unmerged_in_review(
        repo, tmp_path, world):
    rows = (f"| D-505 | demo | one Models: {SEG_A} "
            f"Closes: demo:CODE-1@{repo.merged[:8]} | router | why |\n"
            f"| D-506 | demo | two Models: {SEG_B} "
            f"Closes: demo:CODE-2@{repo.unmerged[:8]} | router | why |\n")
    text, view = decs(tmp_path, rows)
    world.store.ctx.decisions = view
    p = plan.build_plan([], [], [], {"D-505", "D-506"}, live_items(world),
                        view, decisions_text=text, repos=repos(repo))
    assert len(close_ops(p.ops)) == 1
    assert_ops_valid(p.ops)

    batch = tmp_path / "md-sync.yaml"
    batch.write_text(json.dumps({"ops": p.ops}), encoding="utf-8")
    digest = hashlib.sha256(batch.read_bytes()).hexdigest()
    world.store.apply_ops(world.p("router"), p.ops, f"{batch}@{digest}")

    items = {i["key"]: i for i in live_items(world)}
    assert items["demo:CODE-1"]["state"] == "done"
    assert items["demo:CODE-2"]["state"] == "in_review"
    assert items["demo:CODE-1"]["note"] == f"D-505 {SEG_A}"
    assert items["demo:CODE-2"]["note"] == f"D-506 {SEG_B}"
    commit_rows = world.conn.execute(
        "SELECT kind, ref, verified FROM evidence WHERE key='demo:CODE-1'"
        " AND kind='commit'").fetchall()
    assert len(commit_rows) == 1
    assert json.loads(commit_rows[0]["ref"])["sha"] == repo.merged
    assert commit_rows[0]["verified"] == 1
    closed = world.conn.execute(
        "SELECT 1 FROM events WHERE key='demo:CODE-1' AND"
        " type='closed_on_decision'").fetchall()
    assert len(closed) == 1
    not_merged = world.conn.execute(
        "SELECT payload FROM events WHERE key='demo:CODE-2' AND"
        " type='legacy_state_set'").fetchall()
    assert "not on main" in json.dumps([dict(r) for r in not_merged])

    # second run over unchanged input: zero ops (idempotent)
    p2 = plan.build_plan([], [], [], {"D-505", "D-506"}, live_items(world),
                         view, decisions_text=text, repos=repos(repo))
    assert p2.ops == []
    assert p2.d_in_review  # still listed, nothing planned
