"""``GET /v1/alarms`` and ``POST /v1/ack_alarm``: the projector alarms inbox doors.

Fake ``projector_alarms`` rows are written straight into the keeper, then read
and acknowledged over HTTP with the same ``World``/``Doors`` helpers the other
inbox tests use, so the file runs unchanged under ``PG_KEEPER_BACKEND`` (except
the deliberate table drop, which needs a raw SQLite handle).
"""
import sqlite3

import pytest

from door_util import Doors
from keeper_util import World
from plangraph.keeper import db as kdb
from plangraph.inbox.form import PAGE

RAISED = ("INSERT INTO projector_alarms(id, kind, workflow_id, key, detail, raised_at,"
          " cleared_at) VALUES(?,?,?,?,?,?,?)")
needs_sqlite = pytest.mark.skipif(
    kdb.select_backend() != "sqlite", reason="the table is dropped on a raw SQLite handle")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def doors(w, tmp_path):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()


def raise_alarm(w, alarm_id=1, kind="state_drift", workflow_id="card:A", key="demo:A",
                cleared_at=None, detail="drifted"):
    stamp = w.store._iso(w.clock())
    w.conn.execute(RAISED, (alarm_id, kind, workflow_id, key, detail, stamp, cleared_at))
    return alarm_id


def listed(doors, who="router"):
    status, body = doors.http(who, "GET", "/v1/alarms")
    assert status == 200, body
    return body


def ack(doors, alarm_id, who="router"):
    return doors.http(who, "POST", "/v1/ack_alarm", {"alarm_id": alarm_id})


# ------------------------------------------------------------------- reads
def test_lists_open_alarms_with_key_kind_and_age(doors, w):
    raise_alarm(w)
    raise_alarm(w, alarm_id=2, key="demo:B", cleared_at=w.store._iso(w.clock()))
    w.clock.advance(45)
    body = listed(doors)
    assert [row["id"] for row in body["alarms"]] == [1]          # the cleared row never lists
    row = body["alarms"][0]
    assert set(row) == {"id", "key", "kind", "workflow_id", "detail", "raised_at", "age_s"}
    assert (row["key"], row["kind"], row["workflow_id"]) == ("demo:A", "state_drift", "card:A")
    assert row["detail"] == "drifted" and row["age_s"] == 45


def test_any_principal_may_read_alarms_but_only_in_its_own_projects(doors, w):
    raise_alarm(w, alarm_id=1, key="demo:A1")
    raise_alarm(w, alarm_id=2, key="other:B1")
    raise_alarm(w, alarm_id=3, key="")                            # keyless alarm
    for who in ("router", "system", "reader"):                    # _SEES_ALL: all three
        assert [r["key"] for r in listed(doors, who)["alarms"]] == ["demo:A1", "other:B1", ""], who
    assert [row["key"] for row in listed(doors, "l1main")["alarms"]] == ["demo:A1"]
    assert [row["key"] for row in listed(doors, "l1other")["alarms"]] == ["other:B1"]
    assert [row["key"] for row in listed(doors, "reviewer")["alarms"]] == ["demo:A1"]
    assert [row["key"] for row in listed(doors, "workerA")["alarms"]] == ["demo:A1"]


def test_a_principal_without_the_alarm_project_sees_none(doors, w):
    raise_alarm(w, alarm_id=1, key="demo:A1")                     # a demo-only alarm
    assert listed(doors, "l1other")["alarms"] == []               # projects ['other']: none match
    assert [row["id"] for row in listed(doors, "l1main")["alarms"]] == [1]


# ------------------------------------------------------------------- write
def test_ack_removes_the_alarm_and_is_idempotent(doors, w):
    raise_alarm(w)
    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})
    assert listed(doors)["alarms"] == []
    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})   # acknowledged again: ok
    rows = w.conn.execute("SELECT key, ok FROM audit_log WHERE action='ack_alarm'").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("alarm:1", 1), ("alarm:1", 1)]   # the ack IS the audit row


def test_ack_is_router_or_system_only(doors, w):
    raise_alarm(w)
    for who in ("workerA", "l1main", "reviewer"):
        status, body = ack(doors, 1, who)
        assert (status, body["error"]) == (403, "forbidden"), who
    assert [row["id"] for row in listed(doors)["alarms"]] == [1]    # the refusal stored nothing
    assert ack(doors, 1, "router") == (200, {"alarm_id": 1, "acked": True})
    assert ack(doors, 1, "system") == (200, {"alarm_id": 1, "acked": True})


def test_ack_validates_the_alarm_id(doors, w):
    raise_alarm(w)
    for bad in (None, True, "1", 1.5, [1]):
        status, body = ack(doors, bad)
        assert (status, body["error"]) == (400, "validation"), bad
    assert [row["id"] for row in listed(doors)["alarms"]] == [1]


def test_ack_refuses_a_cleared_or_unknown_alarm(doors, w):
    raise_alarm(w, alarm_id=1, cleared_at=w.store._iso(w.clock()))
    raise_alarm(w, alarm_id=2)
    status, body = ack(doors, 1)
    assert (status, body["error"]) == (400, "bad_state")
    status, body = ack(doors, 99)
    assert (status, body["error"]) == (404, "not_found")
    assert [row["id"] for row in listed(doors)["alarms"]] == [2]


def test_a_refused_ack_is_audited_but_keeps_the_alarm_listed(doors, w):
    raise_alarm(w)
    status, body = ack(doors, 1, "workerA")                     # worker may not ack
    assert (status, body["error"]) == (403, "forbidden")
    rows = w.conn.execute("SELECT key, ok, code FROM audit_log WHERE action='ack_alarm'").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("alarm:1", 0, "forbidden")]  # ok=0: not an ack
    assert [row["id"] for row in listed(doors)["alarms"]] == [1]
    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})   # router still can


def test_a_re_raised_alarm_lists_again_once_its_new_id_is_open(doors, w):
    raise_alarm(w, alarm_id=1)
    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})
    assert listed(doors)["alarms"] == []
    w.conn.execute("UPDATE projector_alarms SET cleared_at=? WHERE id=1",
                   (w.store._iso(w.clock()),))
    raise_alarm(w, alarm_id=2)                                  # same kind/workflow/key, new id
    assert [row["id"] for row in listed(doors)["alarms"]] == [2]


@needs_sqlite
def test_a_broken_alarms_query_is_reported_never_hidden(doors, w):
    raise_alarm(w)
    raw = sqlite3.connect(str(w.path), isolation_level=None)
    try:
        raw.execute("DROP TABLE projector_alarms")
    finally:
        raw.close()
    status, body = doors.http("router", "GET", "/v1/alarms")
    assert status == 200 and body["alarms"] == []
    assert body["error"].startswith("alarms unreadable: ")
    assert str(w.path) not in body["error"]                        # no paths, no message text


# -------------------------------------------------------------------- page
def test_page_renders_the_alarm_routes():
    assert "/v1/alarms" in PAGE
    assert "/v1/ack_alarm" in PAGE
