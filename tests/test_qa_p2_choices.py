"""qa-p2 W1: choices edits + Parent edge (synthetic, hermetic)."""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import md_sync
import md_sync_parse as mp
import md_sync_plan as plan

GOAL = {"project": "routing", "id": "GOAL", "key": "routing:GOAL", "state": "open",
        "kind": "goal", "depends_on": ["routing:R-1"], "evidence": []}

STEP = ("say enable after reviewing Options: enable = mail goes out; "
        "own sender = needs a new mailbox; *stay disabled = nothing changes. "
        "Deadline: 2026-10-12")


def live(project, ident, state="open", **kw):
    item = {"project": project, "id": ident, "key": f"{project}:{ident}",
            "state": state, "kind": "question", "title": "", "note": None,
            "description": None, "depends_on": [], "evidence": [],
            "evidence_kinds": ["decision"], "review_tier": "none",
            "source": "md-sync", "wait": "answer"}
    item.update(kw)
    return item


def qtable(row, header="| Prio | ID | Kind | why | step | where |"):
    text = (header + "\n|---|---|---|---|---|---|\n" + row + "\n")
    return mp.parse_questions(text)


def build(entries, items=()):
    return plan.build_plan(list(entries), [], [], set(), [GOAL, *items])


# ---- Task 1 ----
def test_valid_block_item_without_choices_emits_edit():
    (e,) = qtable(f"| 3 | Q-MAIL | ANSWER | why text | {STEP} | any |")
    assert e.choices is not None
    item = live("routing", "Q-MAIL", title=e.title, description=e.description,
                wait=e.wait)
    p = build([e], [item])
    edits = [o for o in p.ops if o["op"] == "edit" and o["key"] == "routing:Q-MAIL"]
    assert len(edits) == 1 and "choices" in edits[0]["fields"]


def test_idempotent_no_op_when_same_choices():
    (e,) = qtable(f"| 3 | Q-MAIL | ANSWER | why text | {STEP} | any |")
    want = plan._masked_choices(e.choices, plan.Plan())
    item = live("routing", "Q-MAIL", title=e.title, description=e.description,
                wait=e.wait, choices=want)
    p = build([e], [item])
    assert p.ops == []


def test_different_options_emits_edit():
    (e,) = qtable(f"| 3 | Q-MAIL | ANSWER | why text | {STEP} | any |")
    other = dict(plan._masked_choices(e.choices, plan.Plan()))
    other["default"] = "enable"
    item = live("routing", "Q-MAIL", title=e.title, description=e.description,
                wait=e.wait, choices=other)
    p = build([e], [item])
    assert any(o["op"] == "edit" and "choices" in o["fields"] for o in p.ops)


def test_non_mdsync_source_still_edited():
    (e,) = qtable(f"| 3 | Q-MAIL | ANSWER | why text | {STEP} | any |")
    item = live("routing", "Q-MAIL", title=e.title, description=e.description,
                wait=e.wait, source="other")
    p = build([e], [item])
    edits = [o for o in p.ops if o.get("key") == "routing:Q-MAIL"]
    assert len(edits) == 1 and "choices" in edits[0]["fields"]


def test_malformed_block_no_op_plus_warning():
    bad_step = "do it Options: a = x; ; b = y"
    (e,) = qtable(f"| 1 | Q-BAD | ANSWER | why | {bad_step} | where |")
    assert e.choices is None
    keep = {"options": [{"id": "k", "label": "K"}], "default": None,
            "deadline": None, "blocks": None}
    item = live("routing", "Q-BAD", title=e.title, description=e.description,
                wait=e.wait, choices=dict(keep))
    p = build([e], [item])
    assert p.ops == []
    assert any("malformed Options block ignored" in w["text"] for w in p.warnings)
    assert item["choices"] == keep


def test_no_block_no_op_keeps_live_choices():
    (e,) = qtable("| 1 | Q-NB | ANSWER | why | just do it | where |")
    assert e.choices is None
    keep = {"options": [{"id": "k", "label": "K"}], "default": None,
            "deadline": None, "blocks": None}
    item = live("routing", "Q-NB", title=e.title, description=e.description,
                wait=e.wait, choices=dict(keep))
    p = build([e], [item])
    assert p.ops == []
    assert not [w for w in p.warnings if "malformed" in w["text"]]


def test_claimed_and_in_review_go_to_choices_skipped_and_report():
    (e,) = qtable(f"| 3 | Q-SK | ANSWER | why text | {STEP} | any |")
    for st in ("claimed", "in_review"):
        item = live("routing", "Q-SK", state=st, title=e.title,
                    description=e.description, wait=e.wait)
        p = build([e], [item])
        assert p.ops == []
        assert (f"routing:Q-SK", st) in p.choices_skipped
        out = io.StringIO()
        md_sync.report(p, out, True)
        assert f"choices-skipped routing:Q-SK: {st}" in out.getvalue()
    # status field
    item = live("routing", "Q-SK", state="claimed")
    p = build([e], [item])
    st = md_sync.status_doc("dry")
    md_sync.fill_status(st, p)
    assert {"key": "routing:Q-SK", "state": "claimed"} in st["choices_skipped"]


def test_legacy_kind_warns():
    (e,) = qtable(f"| 3 | Q-LG | ANSWER | why text | {STEP} | any |")
    item = live("routing", "Q-LG", kind="lane", title=e.title,
                description=e.description, wait=e.wait)
    p = build([e], [item])
    assert p.ops == []
    assert any("choices" in w["text"] for w in p.warnings)


# ---- Task 2 ----
def test_column_parent_token_and_ids():
    text = ("| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-C1 | routing:G1 | why | do it | where |\n")
    (e,) = mp.parse_questions(text)
    assert e.parent == "routing:G1"
    # bare id
    text2 = ("| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
             "| 1 | Q-C2 | PH-OPERATOR | why | do it | where |\n")
    (e2,) = mp.parse_questions(text2)
    assert e2.parent == "PH-OPERATOR"


def test_token_parent_stripped_and_column_wins():
    (e,) = qtable("| 1 | Q-T | ANSWER | why | do it Parent: PH-OPERATOR | where |",
                  header="| Prio | ID | Kind | why | step | where |")
    assert e.parent == "PH-OPERATOR"
    assert "Parent:" not in e.description
    # token preceding Options
    opt_step = "do it Parent: PH-OPERATOR Options: a = x; *b = y"
    (e2,) = qtable(f"| 1 | Q-T2 | ANSWER | why | {opt_step} | where |",
                   header="| Prio | ID | Kind | why | step | where |")
    assert e2.parent == "PH-OPERATOR"
    assert e2.choices is not None and "Parent:" not in e2.description
    # column wins over token
    text = ("| Prio | ID | Kind | Parent | why | step | where |\n|---|---|---|---|---|---|---|\n"
            "| 1 | Q-CW | ANSWER | routing:G1 | why | do it Parent: routing:G2 | where |\n")
    (e3,) = mp.parse_questions(text)
    assert e3.parent == "routing:G1"


def test_two_questions_same_parent_one_edit():
    text = ("| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-A1 | routing:G1 | why | do it | where |\n"
            "| 1 | Q-A2 | routing:G1 | why | do that | where |\n")
    entries = mp.parse_questions(text)
    g1 = live("routing", "G1", kind="gate", title="t", description="d")
    a1 = live("routing", "Q-A1", title=entries[0].title,
              description=entries[0].description, wait=entries[0].wait)
    a2 = live("routing", "Q-A2", title=entries[1].title,
              description=entries[1].description, wait=entries[1].wait)
    # gate already depends on nothing; need gate + goal? gate exists so no goal edit
    gate_item = dict(g1, depends_on=[])
    p = build(entries, [a1, a2, gate_item])
    edits = [o for o in p.ops if o.get("key") == "routing:G1"]
    assert len(edits) == 1
    assert edits[0]["fields"]["depends_on"] == ["routing:Q-A1", "routing:Q-A2"]


def test_parent_gate_with_new_lane_single_edit():
    text = ("| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-NEW | routing:PH-OPERATOR | why | do it | where |\n")
    entries = mp.parse_questions(text)
    gate = live("routing", "PH-OPERATOR", kind="gate", depends_on=["routing:OLD"])
    old = live("routing", "Q-OLD", title="x", description="y", wait="answer")
    # Q-NEW is new; Q-OLD existing to avoid gone noise (still gone? Q-OLD not in questions -> gone, fine)
    p = build(entries, [gate, old])
    edits = [o for o in p.ops if o.get("key") == "routing:PH-OPERATOR"]
    assert len(edits) == 1
    deps = edits[0]["fields"]["depends_on"]
    assert deps.count("routing:Q-NEW") == 1
    assert deps == ["routing:OLD", "routing:Q-NEW"]


def test_parent_idempotent_and_bad_parents_warn():
    text = ("| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-A1 | routing:G1 | why | do it | where |\n")
    entries = mp.parse_questions(text)
    g1 = live("routing", "G1", kind="gate",
              depends_on=["routing:Q-A1"])
    a1 = live("routing", "Q-A1", title=entries[0].title,
              description=entries[0].description, wait=entries[0].wait)
    p = build(entries, [a1, g1])
    assert [o for o in p.ops if o.get("key") == "routing:G1"] == []
    # unknown
    (eu,) = mp.parse_questions(
        "| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
        "| 1 | Q-U | routing:NOPE | why | do it | where |\n")
    au = live("routing", "Q-U", title=eu.title, description=eu.description, wait=eu.wait)
    pu = build([eu], [au])
    assert pu.ops == [] and any("parent routing:NOPE" in w["text"] for w in pu.warnings)
    # non-gate
    ng = live("routing", "NG1", kind="question")
    (en,) = mp.parse_questions(
        "| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
        "| 1 | Q-N | routing:NG1 | why | do it | where |\n")
    an = live("routing", "Q-N", title=en.title, description=en.description, wait=en.wait)
    pn = build([en], [an, ng])
    assert pn.ops == [] and any("parent routing:NG1" in w["text"] for w in pn.warnings)
    # closed gate
    cg = live("routing", "CG1", kind="gate", state="done")
    (ec,) = mp.parse_questions(
        "| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
        "| 1 | Q-C | routing:CG1 | why | do it | where |\n")
    ac = live("routing", "Q-C", title=ec.title, description=ec.description, wait=ec.wait)
    pc = build([ec], [ac, cg])
    assert pc.ops == [] and any("parent routing:CG1" in w["text"] for w in pc.warnings)


def test_kind_only_table_unchanged():
    text = ("| Prio | ID | Kind | Why | Step | Where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-A | TASK | Restart the box | do it | host |\n")
    (e,) = mp.parse_questions(text)
    assert (e.wait, e.title, e.description) == ("task", "Q: Restart the box", "do it — host")
    assert e.parent is None
