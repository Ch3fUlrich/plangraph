"""MK-ANSWER: router verb ``answer`` (question/decision items), event ``answered``.

Mutation killed by these tests: dropping 'answered' from ``schema._LOCKED`` fails
``test_answered_item_is_locked``; skipping the ``Closes:`` check in
``_check_answer_decision`` fails ``test_decision_must_name_the_key``.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from door_util import Doors
from keeper_util import World, defn, to_claimed
from plangraph.keeper import db as kdb, mcp, schema
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.events import verify_integrity, read_events

DID = "D-500"


def _decisions(tmp_path, body):
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Scope |\n|---|---|---|\n" + body, encoding="utf-8")
    return DecisionsAdapter(path)


def _row(key="demo:Q", did=DID):
    return f"| {did} | operator answered | Closes: {key} |\n"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("Q", kind="question", operator=None),
        defn("DEC", kind="decision", operator=None),
        defn("L"), defn("G", ["Q", "DEC", "L"], kind="goal")])
    world.store.ctx = SimpleNamespace(decisions=_decisions(
        tmp_path, _row("demo:Q") + _row("demo:DEC", "D-501") + _row("demo:L", "D-502")
        + "| D-503 | no closes |\n"))
    yield world
    world.close()


def _item(w, key):
    return w.store._item(key)


def test_answer_question_open(w):
    out = w.store.answer(w.p("router"), "demo:Q", "yes, ship it", DID)
    assert out == {"key": "demo:Q", "state": "done", "decision_id": DID}
    item = _item(w, "demo:Q")
    assert item["state"] == "done" and item["reviewer"] == "router"
    ev = [e for e in read_events(w.conn) if e["type"] == "answered"]
    assert len(ev) == 1 and ev[0]["key"] == "demo:Q"
    assert ev[0]["payload"] == {"text": "yes, ship it", "decision_id": DID}
    assert verify_integrity(w.conn) == []
    assert w.store.show(w.p("reader"), "demo:Q")["done_at"] is not None


def test_answer_decision_kind_and_active_states(w):
    to_claimed(w, "demo:DEC")
    w.store.answer(w.p("router"), "demo:DEC", "A", "D-501")
    assert _item(w, "demo:DEC")["state"] == "done"
    assert verify_integrity(w.conn) == []


def test_answered_item_is_locked(w):
    w.store.answer(w.p("router"), "demo:Q", "x", DID)
    for call in (
            lambda: w.store.answer(w.p("router"), "demo:Q", "again", DID),
            lambda: w.store.edit_definition(w.p("router"), "demo:Q", {"title": "t"}),
            lambda: w.store.cancel(w.p("router"), "demo:Q", "no")):
        with pytest.raises(Refused) as exc:
            call()
        assert exc.value.code == "locked"
    raw = sqlite3.connect(str(w.path))
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("INSERT INTO events(ts,key,type,payload,hash) VALUES"
                    "('t','demo:Q','heartbeat','{}','h')")
    raw.close()


@pytest.mark.parametrize("key", ["demo:L", "demo:G"])
def test_only_question_or_decision(w, key):
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p("router"), key, "x", "D-502")
    assert exc.value.code == "validation" and "question" in exc.value.msg
    assert _item(w, key)["state"] == "open"


@pytest.mark.parametrize("who", ["l1main", "workerA", "reviewer", "reader", "system"])
def test_router_only(w, who):
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p(who), "demo:Q", "x", DID)
    assert exc.value.code == "forbidden"
    assert _item(w, "demo:Q")["state"] == "open"


def test_unknown_key(w):
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p("router"), "demo:NOPE", "x", DID)
    assert exc.value.code == "not_found"


@pytest.mark.parametrize("text", ["", "  ", None, 5, ["x"], "x" * 2001])
def test_bad_text(w, text):
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p("router"), "demo:Q", text, DID)
    assert exc.value.code == "validation" and "text" in exc.value.msg
    assert _item(w, "demo:Q")["state"] == "open"


def test_text_cap_boundary_and_credential_guard(w):
    with pytest.raises(Refused):
        w.store.answer(w.p("router"), "demo:DEC", "login " + "admin" + "/" + "hunter22secret", "D-501")
    w.store.answer(w.p("router"), "demo:Q", "x" * 2000, DID)
    assert len(next(e for e in read_events(w.conn)
                    if e["type"] == "answered")["payload"]["text"]) == 2000


@pytest.mark.parametrize("did", [None, "", 5, " D-500", "D-500 ", "D" * 65])
def test_bad_decision_id(w, did):
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p("router"), "demo:Q", "x", did)
    assert exc.value.code == "validation"


def test_decision_must_name_the_key(w):
    for did in ("D-501", "D-503", "D-999"):  # names another key / no Closes / absent
        with pytest.raises(Refused) as exc:
            w.store.answer(w.p("router"), "demo:Q", "x", did)
        assert exc.value.code == "evidence_unverified"
    assert _item(w, "demo:Q")["state"] == "open"


def test_no_decisions_adapter_fails_closed(w):
    w.store.ctx = SimpleNamespace(decisions=None)
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p("router"), "demo:Q", "x", DID)
    assert exc.value.code == "evidence_unverified"


def test_cancelled_item_refused(w):
    w.store.cancel(w.p("router"), "demo:Q", "dup")
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p("router"), "demo:Q", "x", DID)
    assert exc.value.code == "locked"


def test_unlocks_dependents(w):
    w.store.create_items(w.p("router"), [defn("DEP", ["Q"]),
                                         defn("G2", ["DEP"], kind="goal")])
    assert "demo:DEP" not in w.store.ready(w.p("reader"))
    w.store.answer(w.p("router"), "demo:Q", "go", DID)
    assert "demo:DEP" in w.store.ready(w.p("reader"))


def test_http_and_mcp_doors(w, tmp_path):
    doors = Doors(w, tmp_path)
    try:
        status, body = doors.http("l1main", "POST", "/v1/answer",
                                  {"key": "demo:Q", "text": "x", "decision_id": DID})
        assert status == 403
        status, body = doors.http("router", "POST", "/v1/answer",
                                  {"key": "demo:Q", "text": "ok", "decision_id": DID})
        assert status == 200 and body["state"] == "done"
        status, _ = doors.http("router", "POST", "/v1/answer", {"key": "demo:Q"})
        assert status in (400, 422)
    finally:
        doors.close()
    assert mcp._ROUTES["answer"] == ("POST", "answer")
    tools = {t["name"]: t for t in mcp.TOOLS}
    assert tools["answer"]["inputSchema"]["required"] == ["key", "text", "decision_id"]


def test_event_type_registered():
    assert "answered" in schema.EVENT_TYPES


def test_old_database_gains_answered_lock(tmp_path):
    """A DB created before 'answered' locked nothing on it; opening upgrades the triggers."""
    path = tmp_path / "old.db"
    kdb.open_db(path, create=True).close()
    raw = sqlite3.connect(str(path))
    names = [r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%locked%'")]
    for name in names:
        sql = raw.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        raw.execute(f"DROP TRIGGER {name}")
        raw.execute(sql.replace(",'answered'", ""))
    raw.commit()
    assert all("'answered'" not in r[0] for r in raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%locked%'"))
    raw.close()
    for _ in range(2):  # the second open is a no-op
        conn = kdb.open_db(path)
        rows = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"
                            " AND name LIKE 'trg_%locked%'").fetchall()
        assert len(rows) == len(names) == 10
        assert all("'answered'" in r["sql"] for r in rows)
        conn.close()
