"""``GET /v1/projector_watermark`` (RT-P1-PROJECTOR gap P5): read-only, same auth gate as the
other inbox reads, one watermark line on the inbox page.  Runs on whichever backend
``PG_KEEPER_BACKEND`` selects."""
import urllib.error
import urllib.request

import pytest

from door_util import Doors
from keeper_util import World
from plangraph.inbox.form import PAGE

WM = ("INSERT INTO projector_watermark(workflow_id, run_id, run_ord, event_id, status, plan_key,"
      " updated_at) VALUES(?,?,?,?,?,?,?)")


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


def get(doors, who="router"):
    return doors.http(who, "GET", "/v1/projector_watermark")


def test_empty_watermark_is_a_named_null_not_an_error(doors):
    status, body = get(doors)
    assert status == 200, body
    assert body == {"global": None, "workflows": [], "open_alarms": 0}


def test_reports_global_workflow_rows_and_open_alarm_count(doors, w):
    stamp = w.store._iso(w.clock())
    w.conn.execute(WM, ("*", "", 0, 0, "", "", stamp))
    w.conn.execute(WM, ("card:demo:A", "r1", 0, 9, "completed", "demo:A", stamp))
    w.conn.execute(WM, ("card:other:B", "r2", 1, 4, "running", "other:B", stamp))
    for i, cleared in ((1, None), (2, None), (3, stamp)):
        w.conn.execute("INSERT INTO projector_alarms(id, kind, workflow_id, key, detail, raised_at,"
                       " cleared_at) VALUES(?,?,?,?,?,?,?)",
                       (i, "state_drift", "wf", f"k{i}", "d", stamp, cleared))
    w.clock.advance(30)
    status, body = get(doors)
    assert status == 200, body
    assert body["global"] == {"updated_at": stamp, "age_s": 30}
    assert [r["workflow_id"] for r in body["workflows"]] == ["card:demo:A", "card:other:B"]
    assert body["workflows"][0] == {
        "workflow_id": "card:demo:A", "run_id": "r1", "run_ord": 0, "event_id": 9,
        "status": "completed", "plan_key": "demo:A", "updated_at": stamp}
    assert body["open_alarms"] == 2                      # the cleared row does not count


def test_non_privileged_caller_sees_only_its_projects(doors, w):
    stamp = w.store._iso(w.clock())
    w.conn.execute(WM, ("card:demo:A", "r1", 0, 9, "completed", "demo:A", stamp))
    w.conn.execute(WM, ("card:other:B", "r2", 0, 4, "running", "other:B", stamp))
    assert len(get(doors, "reader")[1]["workflows"]) == 2
    assert [r["plan_key"] for r in get(doors, "l1main")[1]["workflows"]] == ["demo:A"]
    assert [r["plan_key"] for r in get(doors, "l1other")[1]["workflows"]] == ["other:B"]


def test_read_only_does_not_write_state(doors, w):
    before = w.conn.execute("SELECT COUNT(*) FROM projector_watermark").fetchone()[0]
    get(doors)
    assert w.conn.execute("SELECT COUNT(*) FROM projector_watermark").fetchone()[0] == before


def test_unauthenticated_call_is_refused(doors):
    for headers in ({}, {"Authorization": "Bearer nope"}):
        req = urllib.request.Request(doors.url + "/v1/projector_watermark", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 401


def test_post_is_not_a_door_for_the_read_verb(doors):
    status, _ = doors.http("router", "POST", "/v1/projector_watermark", {})
    assert status in (404, 405)


def test_page_shows_one_watermark_line():
    assert 'id="wm"' in PAGE and "/v1/projector_watermark" in PAGE
