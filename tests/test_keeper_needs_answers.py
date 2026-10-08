"""MK-READS: read verbs ``needs_you`` and ``answers`` (pure views + thin doors).

Mutation killed by these tests: ranking by priority before unblocks fails
``test_needs_you_ranked_by_unblocks_then_priority``; dropping the ``corrects``
join fails ``test_answers_superseded_by``.
"""
from types import SimpleNamespace

import pytest

from door_util import Doors
from keeper_util import World, defn, seed
from plangraph import views
from plangraph.keeper import fmt, mcp
from plangraph.keeper.evidence_decisions import DecisionsAdapter


def _it(key, **kw):
    item = {"key": key, "project": key.split(":")[0], "title": key, "state": "open",
            "depends_on": [], "priority": 0, "kind": "lane", "operator": False}
    item.update(kw)
    return item


# ------------------------------------------------------------------ pure views
def test_needs_you_selection():
    items = [_it("p:L"), _it("p:OP", operator=True), _it("p:Q", kind="question"),
             _it("p:D", kind="decision"), _it("p:T", kind="operator_task"),
             _it("p:DONE", kind="question", state="done"),
             _it("p:CAN", kind="question", state="cancelled"),
             _it("q:Q2", kind="question")]
    assert [r["key"] for r in views.needs_you(items)] == [
        "p:D", "p:OP", "p:Q", "p:T", "q:Q2"]
    assert [r["key"] for r in views.needs_you(items, project="q")] == ["q:Q2"]


def test_needs_you_ranked_by_unblocks_then_priority():
    items = [_it("p:A", kind="question", priority=9),
             _it("p:B", kind="question", priority=1),
             _it("p:C", kind="question", priority=5),
             _it("p:X1", depends_on=["p:B"]), _it("p:X2", depends_on=["p:B"]),
             _it("p:X3", depends_on=["p:C"]),
             _it("p:X4", depends_on=["p:C"], state="done")]  # terminal: not counted
    rows = views.needs_you(items)
    assert [(r["key"], r["unblocks"]) for r in rows] == [
        ("p:B", 2), ("p:C", 1), ("p:A", 0)]


def test_needs_you_row_shape():
    row = views.needs_you([_it("p:Q", kind="question", wait="answer", priority=3)])[0]
    assert row == {"key": "p:Q", "project": "p", "title": "p:Q", "kind": "question",
                   "state": "open", "wait": "answer", "priority": 3, "unblocks": 0}


def _ans(key, text, did, at, **kw):
    return _it(key, kind="question", state="done", answer={"text": text, "decision_id": did},
               done_at=at, **kw)


def test_answers_newest_first_and_filters():
    items = [_ans("p:A", "a", "D-1", "2026-10-01T00:00:00Z"),
             _ans("p:B", "b", "D-2", "2026-10-03T00:00:00Z"),
             _ans("q:C", "c", "D-3", "2026-10-02T00:00:00Z"),
             _it("p:L", state="done", answer={"text": "x", "decision_id": "D-9"},
                 done_at="2026-10-09T00:00:00Z"),            # not question/decision
             _it("p:OPEN", kind="question"),                   # not done
             _it("p:NOANS", kind="decision", state="done")]    # done without answer
    assert [r["key"] for r in views.answers(items)] == ["p:B", "q:C", "p:A"]
    assert [r["key"] for r in views.answers(items, project="p")] == ["p:B", "p:A"]
    row = views.answers(items)[0]
    assert row == {"key": "p:B", "project": "p", "title": "p:B", "kind": "question",
                   "answer": "b", "decision_id": "D-2", "done_at": "2026-10-03T00:00:00Z"}


def test_answers_superseded_by():
    items = [_ans("p:A", "a", "D-1", "2026-10-01T00:00:00Z"),
             _ans("p:B", "b", "D-2", "2026-10-02T00:00:00Z"),
             _it("p:FIX1", corrects="p:A"), _it("p:FIX2", corrects="p:A"),
             _it("p:GONE", corrects="p:B", state="cancelled")]
    rows = {r["key"]: r for r in views.answers(items)}
    assert rows["p:A"]["superseded_by"] == ["p:FIX1", "p:FIX2"]
    assert "superseded_by" not in rows["p:B"]


def test_answers_same_timestamp_ordered_by_key():
    items = [_ans("p:B", "b", "D-2", "t"), _ans("p:A", "a", "D-1", "t")]
    assert [r["key"] for r in views.answers(items)] == ["p:A", "p:B"]


def test_texts():
    assert fmt.needs_you_text([]) == "nothing needs you"
    assert fmt.answers_text([]) == "no answers"
    rows = views.answers([_ans("p:A", "yes", "D-1", "t"), _it("p:F", corrects="p:A")])
    assert fmt.answers_text(rows) == "p:A | D-1 | t | yes | superseded_by p:F"
    assert "p:Q | p:Q | question answer | unblocks 0 | prio 0" in fmt.needs_you_text(
        views.needs_you([_it("p:Q", kind="question", wait="answer")]))


# ------------------------------------------------------------- store and doors
@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("Q", kind="question", operator=None, project="demo"),
        defn("Q2", kind="decision", operator=None, project="demo"),
        defn("T", kind="operator_task", operator=None, project="demo"),
        defn("L"), defn("OTHER", project="other"),
        defn("WORK", ["Q"]),
        defn("G", ["Q", "Q2", "T", "L", "WORK"], kind="goal"),
        defn("GO", ["OTHER"], kind="goal", project="other")])
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Scope |\n|---|---|---|\n"
                    "| D-1 | a | Closes: demo:Q |\n| D-2 | b | Closes: demo:Q2 |\n",
                    encoding="utf-8")
    world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    yield world
    world.close()


def test_store_needs_you(w):
    rows = w.store.needs_you(w.p("reader"))
    assert [r["key"] for r in rows] == ["demo:Q", "demo:Q2", "demo:T"]
    assert rows[0]["unblocks"] == 2 and rows[0]["wait"] == "answer"
    assert rows[2]["wait"] == "task"
    assert w.store.needs_you(w.p("reader"), project="other") == []
    w.store.answer(w.p("router"), "demo:Q", "yes", "D-1")
    assert [r["key"] for r in w.store.needs_you(w.p("reader"))] == ["demo:Q2", "demo:T"]


def test_store_answers_and_supersede(w):
    assert w.store.answers(w.p("reader")) == []
    w.store.answer(w.p("router"), "demo:Q", "yes", "D-1")
    w.clock.advance(60)
    w.store.answer(w.p("router"), "demo:Q2", "no", "D-2")
    rows = w.store.answers(w.p("reader"))
    assert [(r["key"], r["answer"], r["decision_id"]) for r in rows] == [
        ("demo:Q2", "no", "D-2"), ("demo:Q", "yes", "D-1")]
    assert rows[0]["done_at"] > rows[1]["done_at"]
    w.store.create_items(w.p("router"), [
        defn("REDO", kind="question", corrects="demo:Q", operator=None),
        defn("G3", ["REDO"], kind="goal")])
    got = {r["key"]: r for r in w.store.answers(w.p("reader"))}
    assert got["demo:Q"]["superseded_by"] == ["demo:REDO"]
    assert "superseded_by" not in got["demo:Q2"]
    assert w.store.answers(w.p("reader"), project="other") == []


def test_http_and_mcp_doors(w, tmp_path):
    w.store.answer(w.p("router"), "demo:Q", "yes", "D-1")
    doors = Doors(w, tmp_path)
    try:
        status, body = doors.http("reader", "GET", "/v1/needs_you")
        assert status == 200 and [r["key"] for r in body] == ["demo:Q2", "demo:T"]
        status, body = doors.http("reader", "GET", "/v1/needs_you?project=other")
        assert status == 200 and body == []
        status, body = doors.http("reader", "GET", "/v1/answers")
        assert status == 200 and body[0]["key"] == "demo:Q" and body[0]["answer"] == "yes"
        status, text = doors.http("reader", "GET", "/v1/answers?fmt=text")
        assert status == 200 and text.startswith("demo:Q | D-1 |")
        status, _ = doors.http("reader", "POST", "/v1/needs_you", {})
        assert status in (400, 404, 405)
    finally:
        doors.close()
    assert mcp._ROUTES["needs_you"] == ("GET", "needs_you")
    assert mcp._ROUTES["answers"] == ("GET", "answers")
    assert {"needs_you", "answers"} <= {t["name"] for t in mcp.TOOLS}
