"""W13 md-sync wait: table rows + suffixed ids + wait on create/edit (synthetic).

Mutation killed by these tests: dropping ``wait`` from the create lane in
``md_sync_plan.build_plan`` makes ``test_create_carries_wait`` fail (the
lane for OS-124B carries no ``wait`` instead of ``"task"``).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import md_sync_parse as mp  # noqa: E402
import md_sync_plan as plan  # noqa: E402

QFIX = (
    "| 1 | OS-124b | Migrate the cache slowly. Extra detail. "
    "| Run the migration | In router docs |\n"
    "| 2 | OS-70 (3b) | Should we retry on timeout? Extra. "
    "| Check the flag | In config |\n"
    "| 3 | OS-56, OS-51 | Skipped multi row. | Do it | Somewhere |\n"
    "| 4 | OS-90 | Table title here. | Table step | Table place |\n"
    "\n"
    "## Operator steps\n"
    "- OS-90 **Bullet title wins** with extra detail\n"
    "- Q-ASK **Should we ship?** with detail\n"
    "- OS-91 decide the rotation soon\n"
)

GOAL = {"project": "routing", "id": "GOAL", "key": "routing:GOAL", "state": "open",
        "kind": "goal", "depends_on": ["routing:R-1"], "evidence": []}


def live(project, ident, state="open", **kw):
    item = {"project": project, "id": ident, "key": f"{project}:{ident}",
            "state": state, "kind": "lane", "title": "", "note": None,
            "description": None, "depends_on": [], "evidence": [],
            "evidence_kinds": ["decision"], "review_tier": "none",
            "source": "md-sync", "wait": None}
    item.update(kw)
    return item


def questions():
    return mp.parse_questions(QFIX)


def by_id(entries):
    return {e.id: e for e in entries}


def build(entries, items=()):
    return plan.build_plan(list(entries), [], [], set(),
                           [GOAL, *items])


def test_table_rows_parsed_suffixed_id_uppercased():
    ids = [e.id for e in questions()]
    assert ids == ["OS-124B", "OS-70", "OS-90", "Q-ASK", "OS-91"]
    assert "OS-56" not in ids and "OS-51" not in ids  # multi-id row skipped
    assert "OS-124b" not in ids


def test_bullet_wins_over_table_row():
    e = by_id(questions())
    assert e["OS-90"].title == "OP: Bullet title wins"
    assert e["OS-90"].description == "with extra detail"


def test_table_title_and_description_shape():
    e = by_id(questions())
    assert e["OS-124B"].title == "OP: Migrate the cache slowly."
    assert e["OS-124B"].description == "Run the migration — In router docs"
    assert e["OS-70"].title == "OP: Should we retry on timeout?"
    assert e["OS-70"].description == "Check the flag — In config"


def test_wait_classification():
    e = by_id(questions())
    assert e["OS-124B"].wait == "task"
    assert e["OS-90"].wait == "task"
    assert e["OS-70"].wait == "answer"   # "?" in the title
    assert e["Q-ASK"].wait == "answer"    # Q- id (also "?")
    assert e["OS-91"].wait == "answer"    # row text starts with "decide"


def test_create_carries_wait():
    p = build(questions())
    lanes = {o["item"]["id"]: o["item"] for o in p.ops if o["op"] == "create"
             and o["item"]["id"] != "PH-OPERATOR"}
    assert lanes["OS-124B"]["wait"] == "task"
    assert lanes["OS-90"]["wait"] == "task"
    assert lanes["OS-70"]["wait"] == "answer"
    assert lanes["Q-ASK"]["wait"] == "answer"
    assert lanes["OS-91"]["wait"] == "answer"


def test_edit_when_wait_changed_and_claimed_skipped():
    entries = by_id(questions())
    wrong = live("routing", "OS-124B", title=entries["OS-124B"].title,
                 description=entries["OS-124B"].description, wait="answer")
    claimed = live("routing", "OS-70", title=entries["OS-70"].title,
                   description=entries["OS-70"].description, wait="task",
                   state="claimed")
    rest = [live("routing", i, title=entries[i].title,
                 description=entries[i].description, wait=entries[i].wait)
            for i in ("OS-90", "Q-ASK", "OS-91")]
    gate = live("routing", "PH-OPERATOR", kind="gate",
                depends_on=[f"routing:{i}" for i in entries])
    p = build(questions(), [wrong, claimed, *rest, gate])
    edits = {o["key"]: o["fields"] for o in p.ops if o["op"] == "edit"}
    assert edits == {"routing:OS-124B": {"wait": "task"}}
    assert all(o.get("key") != "routing:OS-70" for o in p.ops)


def test_second_run_is_empty():
    entries = by_id(questions())
    items = [live("routing", i, title=entries[i].title,
                  description=entries[i].description, wait=entries[i].wait)
             for i in entries]
    assert build(questions(), items).ops == []


def test_kind_column_sets_wait_and_keeps_the_other_cells():
    """QUESTIONS.md 2026-10-09: ``| Prio | ID | Kind | why | step | where |``."""
    text = ("| Prio | ID | Kind | Why | Step | Where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-A | TASK | Restart the box | do it | host |\n"
            "| 2 | Q-B | ANSWER | Pick a name? | reply | chat |\n"
            "| 3 | OS-9 | answer | Choose | x | y |\n")
    got = {q.id: (q.wait, q.title, q.description) for q in mp.parse_questions(text)}
    assert got == {"Q-A": ("task", "Q: Restart the box", "do it — host"),
                   "Q-B": ("answer", "Q: Pick a name?", "reply — chat"),
                   "OS-9": ("answer", "OP: Choose", "x — y")}


def test_old_five_column_table_still_classifies_by_rule():
    text = "| 1 | OS-5 | Restart the box | do it | host |\n"
    (q,) = mp.parse_questions(text)
    assert (q.wait, q.title, q.description) == ("task", "OP: Restart the box", "do it — host")


def test_five_column_row_whose_why_is_task_is_not_shifted():
    (q,) = mp.parse_questions("| 1 | Q-1 | TASK | Actual step | Actual where |\n")
    assert (q.title, q.description) == ("Q: TASK", "Actual step — Actual where")


def test_kind_header_shifts_even_an_unknown_kind_value():
    text = ("| Prio | ID | Kind | Why | Step | Where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-1 | todo | Why text | Step text | Where text |\n")
    (q,) = mp.parse_questions(text)
    assert (q.title, q.description, q.wait) == ("Q: Why text", "Step text — Where text", "answer")


def test_kind_header_does_not_leak_into_the_next_table():
    text = ("| Prio | ID | Kind | Why | Step | Where |\n|---|---|---|---|---|---|\n"
            "| 1 | Q-1 | TASK | a | b | c |\n\nprose\n\n"
            "| 1 | OS-2 | Restart the box | do it | host |\n")
    got = {q.id: (q.title, q.wait) for q in mp.parse_questions(text)}
    assert got == {"Q-1": ("Q: a", "task"), "OS-2": ("OP: Restart the box", "task")}
