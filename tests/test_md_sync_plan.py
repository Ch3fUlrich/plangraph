"""md_sync v1: mapping parsed markdown + a live snapshot to keeper ops (pure)."""
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import md_sync_parse as mp  # noqa: E402
import md_sync_plan as plan  # noqa: E402

DEC = {"D-10", "D-11", "D-12"}
GOAL = {"project": "routing", "id": "GOAL", "key": "routing:GOAL", "state": "open",
        "kind": "goal", "depends_on": ["routing:R-1"], "evidence": []}


def live(project, ident, state="open", **kw):
    item = {"project": project, "id": ident, "key": f"{project}:{ident}",
            "state": state, "kind": "lane", "title": "", "note": None,
            "description": None,
            "depends_on": [], "evidence": [], "evidence_kinds": ["any"],
            "review_tier": "standard", "source": None}
    item.update(kw)
    return item


def q(text):
    return mp.parse_questions("## Questions\n" + text)


def a(text, date="2026-10-01"):
    return mp.parse_answers(f"## {date}\n" + text)


def build(questions=(), answers=((), ()), items=(), decisions=DEC):
    return plan.build_plan(list(questions), answers[0], answers[1], decisions,
                           [GOAL, *items])


def names(p):
    return [(o["op"], o.get("key") or o["item"]["id"]) for o in p.ops]


def test_new_question_creates_lane_gate_and_goal_link():
    p = build(q("- Q-N1 **New thing** more words\n- Q-N2 **Second**\n"))
    creates = [o["item"] for o in p.ops if o["op"] == "create"]
    lane = creates[0]
    assert lane == {
        "project": "routing", "id": "Q-N1", "title": "Q: New thing", "kind": "lane",
        "track": "QUESTIONS", "owner": "operator", "operator": True,
        "evidence_kinds": ["decision"], "review_tier": "none", "depends_on": [],
        "description": "more words", "source": "md-sync"}
    gate = creates[-1]
    assert gate["id"] == "PH-OPERATOR" and gate["kind"] == "gate"
    assert gate["depends_on"] == ["routing:Q-N1", "routing:Q-N2"]
    goal = [o for o in p.ops if o["op"] == "edit"]
    assert goal == [{"op": "edit", "key": "routing:GOAL", "fields": {
        "depends_on": ["routing:R-1", "routing:PH-OPERATOR"]}}]
    assert p.counts["create"] == 3 and p.counts["edit"] == 1


def test_no_description_omits_the_field():
    p = build(q("- Q-N1 **Only title**\n"))
    assert "description" not in p.ops[0]["item"]


def test_existing_id_in_other_project_is_reused_not_created():
    p = build(q("- Q-SH **Shared**\n"), items=[live("server", "Q-SH")])
    assert p.ops == []


def test_gate_created_once_then_lanes_appended_without_goal_edit():
    gate = live("routing", "PH-OPERATOR", kind="gate", depends_on=["routing:Q-OLD"])
    p = build(q("- Q-N1 **New**\n"), items=[gate, live("routing", "Q-OLD")])
    assert names(p) == [("create", "Q-N1"), ("edit", "routing:PH-OPERATOR")]
    assert p.ops[1]["fields"] == {"depends_on": ["routing:Q-OLD", "routing:Q-N1"]}


def test_missing_goal_is_a_plan_error():
    with pytest.raises(plan.PlanError):
        plan.build_plan(q("- Q-N1 **New**\n"), [], [], DEC, [])


def test_gate_not_open_is_a_plan_error():
    gate = live("routing", "PH-OPERATOR", state="done", kind="gate")
    with pytest.raises(plan.PlanError):
        build(q("- Q-N1 **New**\n"), items=[gate])


def test_open_unchanged_is_quiet_and_changed_edits_ours_only():
    ours = live("routing", "Q-N1", title="Q: Old title", source="md-sync",
                description="stale")
    foreign = live("server", "Q-F", title="curated title")
    p = build(q("- Q-N1 **New title** x\n- Q-F **other words**\n"), items=[ours, foreign])
    assert p.ops == [{"op": "edit", "key": "routing:Q-N1",
                      "fields": {"title": "Q: New title", "description": "x"}}]
    same = live("routing", "Q-N1", title="Q: New title", source="md-sync",
                description="x")
    assert build(q("- Q-N1 **New title** x\n"), items=[same]).ops == []


def test_pre_model_names_item_is_quiet_on_its_folded_note_line():
    # No column yet: the ``description:`` line in note says the same thing,
    # so a re-run emits nothing (idempotence across MODEL-NAMES).
    folded = live("routing", "Q-N1", title="Q: New title", source="md-sync",
                  note="description: x")
    assert build(q("- Q-N1 **New title** x\n"), items=[folded]).ops == []
    changed = live("routing", "Q-N1", title="Q: New title", source="md-sync",
                   note="description: stale")
    p = build(q("- Q-N1 **New title** x\n"), items=[changed])
    assert p.ops == [{"op": "edit", "key": "routing:Q-N1",
                      "fields": {"description": "x"}}]


def test_change_detection_reads_the_column_over_a_leftover_note_line():
    same = live("routing", "Q-N1", title="Q: New title", source="md-sync",
                description="x", note="description: stale")
    assert build(q("- Q-N1 **New title** x\n"), items=[same]).ops == []
    gone = live("routing", "Q-N1", title="Q: New title", source="md-sync",
                description="stale", note=None)
    p = build(q("- Q-N1 **New title** x\n"), items=[gone])
    assert p.ops == [{"op": "edit", "key": "routing:Q-N1",
                      "fields": {"description": "x"}}]


def test_answer_closes_open_item_with_full_sequence():
    item = live("server", "Q-A")
    p = build(answers=a("- Q-A choice: yes (D-10, D-11)\n"), items=[item])
    assert [o["op"] for o in p.ops] == [
        "edit", "legacy_state", "add_evidence", "add_evidence", "close_on_decision"]
    assert p.ops[0]["fields"] == {"review_tier": "none", "evidence_kinds": ["decision"]}
    assert p.ops[1] == {"op": "legacy_state", "key": "server:Q-A",
                        "state": "in_review", "note": "md-sync: answered by D-10, D-11"}
    assert [o["ref"] for o in p.ops[2:4]] == [{"id": "D-10"}, {"id": "D-11"}]
    assert p.counts["close_on_decision"] == 1


def test_answer_skips_edit_when_already_decision_tier():
    item = live("routing", "Q-A", review_tier="none", evidence_kinds=["decision"])
    p = build(answers=a("- Q-A choice: yes (D-10)\n"), items=[item])
    assert [o["op"] for o in p.ops] == ["legacy_state", "add_evidence",
                                        "close_on_decision"]


def test_answer_for_in_review_item_only_adds_evidence_and_closes():
    item = live("routing", "Q-A", state="in_review", review_tier="none",
                evidence_kinds=["decision"],
                evidence=[{"kind": "decision", "ref": {"id": "D-10"}}])
    p = build(answers=a("- Q-A choice: yes (D-10, D-11)\n"), items=[item])
    assert [(o["op"], o.get("ref")) for o in p.ops] == [
        ("add_evidence", {"id": "D-11"}), ("close_on_decision", None)]


def test_answer_for_done_item_is_quiet():
    p = build(answers=a("- Q-A choice: yes (D-10)\n"), items=[live("x", "Q-A", "done")])
    assert p.ops == [] and p.skipped == [("Q-A", "already done")]


def test_answer_for_active_non_decision_item_is_blocked_not_edited():
    item = live("x", "Q-A", state="claimed")
    p = build(answers=a("- Q-A choice: yes (D-10)\n"), items=[item])
    assert p.ops == [] and p.skipped == [("Q-A", "blocked: x:Q-A is claimed")]


def test_unknown_decision_id_refused_without_ops():
    p = build(answers=a("- Q-A choice: yes (D-10, D-999)\n"), items=[live("x", "Q-A")])
    assert p.ops == [] and p.refused == [("Q-A", "unknown decision D-999")]


def test_answer_without_d_id_is_skipped_and_reported():
    p = build(answers=a("- Q-A nothing cited: ok\n"), items=[live("x", "Q-A")])
    assert p.ops == [] and ("Q-A", "no D-id") in p.skipped


def test_answer_for_unknown_item_is_skipped():
    p = build(answers=a("- Q-ZZ nothing live: ok (D-10)\n"))
    assert p.ops == [] and p.skipped == [("Q-ZZ", "no live item")]


def test_answered_new_question_is_created_and_closed_in_one_run():
    p = build(q("- Q-N1 **Both**\n"), answers=a("- Q-N1 done: ok (D-10)\n"))
    kinds = [o["op"] for o in p.ops]
    assert kinds == ["create", "create", "edit", "legacy_state", "add_evidence",
                     "close_on_decision"]


def test_gone_from_questions_is_reported_never_cancelled():
    gone = live("routing", "Q-GONE", source="md-sync")
    p = build(q("- Q-N1 **New**\n"), items=[gone,
              live("routing", "PH-OPERATOR", kind="gate")])
    assert p.gone == ["routing:Q-GONE"]
    assert all(o["op"] != "cancel" for o in p.ops)


def test_duplicate_question_ids_first_wins():
    p = build(q("- Q-N1 **One**\n- Q-N1 **Two**\n"))
    assert p.ops[0]["item"]["title"] == "Q: One"
    assert p.skipped == [("Q-N1", "duplicate in QUESTIONS")]


def test_redaction_counts_and_masks_title_and_description():
    p = build(q("- Q-N1 **Rotate** token: Zx81kQ92LmPq77Vb now\n"))
    item = p.ops[0]["item"]
    assert "Zx81" not in item["description"] and p.redactions == 1


def test_long_description_is_capped_for_the_description_column():
    p = build(q("- Q-N1 **T** " + "word " * 1000 + "\n"))
    desc = p.ops[0]["item"]["description"]
    assert len(desc) <= plan.DESCRIPTION_CAP and desc.endswith("…")


def test_legacy_note_line_reads_back_the_keeper_fold():
    sys.path.insert(0, str(ROOT / "src"))
    from plangraph.keeper.note_fold import fold_note
    folded = fold_note(None, {"description": "hello\nworld"}, "k")
    assert plan.live_description({"description": None, "note": folded}) == "hello\nworld"
    assert plan.live_description({"description": "", "note": folded}) == "hello\nworld"
    assert plan.live_description({"description": None, "note": None}) == ""
    assert plan.live_description({"description": None, "note": "just a note"}) == ""


def test_counts_is_a_counter():
    assert isinstance(build().counts, Counter)


def test_description_of_exactly_the_cap_is_kept_and_one_more_is_cut():
    room = plan.DESCRIPTION_CAP
    exact = "ab " * (room // 3)
    exact += "c" * (room - len(exact))
    assert len(exact) == room and plan._fit(exact) == exact
    cut = plan._fit(exact + "d")
    assert cut.endswith("…") and len(cut) <= room


def test_gone_item_that_is_a_gate_dependency_is_reported_and_dep_kept():
    gone = live("routing", "Q-GONE", source="md-sync")
    gate = live("routing", "PH-OPERATOR", kind="gate", depends_on=["routing:Q-GONE"])
    p = build(q("- Q-N1 **New**\n"), items=[gone, gate])
    assert p.gone == ["routing:Q-GONE"]
    assert p.ops[-1] == {"op": "edit", "key": "routing:PH-OPERATOR", "fields": {
        "depends_on": ["routing:Q-GONE", "routing:Q-N1"]}}
    assert [o["op"] for o in p.ops] == ["create", "edit"]
