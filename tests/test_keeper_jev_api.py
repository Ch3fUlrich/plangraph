"""Contract tests for the Jev shadow verbs over the HTTP door (RT-P1-JEV-SHADOW).

The plan item's public surface is three routes on the keeper API:

* ``POST /v1/jev_shadow``  -> 200 ``{id, card, kind}``   (store: ``jev_record``)
* ``POST /v1/jev_outcome`` -> 200 ``{card, updated}``    (store: ``jev_outcome``)
* ``GET  /v1/jev_report[?project=P][&fmt=text]``          (store: ``jev_report``)

The store methods are unit-tested in ``tests/test_keeper_jev_store.py`` and the
``show`` rendering in ``tests/test_keeper_jev_show.py``; this file only proves the
verbs are wired to real HTTP on 127.0.0.1, that a reader may read but not write,
and that the refusals map to the documented statuses.  All SQL here is portable,
so the file runs unchanged on SQLite and Postgres.
"""
import json
import urllib.error
import urllib.request

import pytest

from door_util import Doors
from keeper_util import World, seed

FOUR_KINDS = {"harness", "window", "budget", "review"}
ALL_KINDS = FOUR_KINDS | {"qc_accept"}


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


def rec(card, **over):
    """A minimal valid ``jev_record`` body for *card*, with *over* applied."""
    body = {"card": card, "kind": "harness", "rule_answer": "rules",
            "jev_answer": "rules", "status": "ok"}
    body.update(over)
    return body


def shadows(w):
    return w.conn.execute("SELECT COUNT(*) FROM jev_shadow").fetchone()[0]


def unauth_status(url, data=None, method="GET"):
    """The status of one request with NO Authorization header."""
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ------------------------------------------------------------------ record
def test_router_records_a_shadow_row_visible_in_show_and_text(doors, w):
    key, = seed(w, [("A", [])])
    status, body = doors.http("router", "POST", "/v1/jev_shadow", rec(key))
    assert status == 200
    assert set(body) == {"id", "card", "kind"}
    assert (body["card"], body["kind"]) == (key, "harness")
    assert shadows(w) == 1

    status, shown = doors.http("router", "GET", f"/v1/show?key={key}&fmt=json")
    assert status == 200
    assert shown["jev_shadow"][0]["kind"] == "harness"
    assert shown["jev_shadow"][0]["agree"] is True
    assert shown["jev_agreement"]["agree"] == 1
    assert shown["jev_agreement"]["agreement_rate"] == 1.0

    status, text = doors.http("router", "GET", f"/v1/show?key={key}&fmt=text")
    assert status == 200
    assert "jev harness" in text
    assert "jev agree 1/1" in text


# ----------------------------------------------------------------- outcome
def test_outcome_is_first_write_wins_and_shows_on_the_row(doors, w):
    key, = seed(w, [("A", [])])
    doors.http("router", "POST", "/v1/jev_shadow", rec(key, attempt=1))
    doors.http("router", "POST", "/v1/jev_shadow", rec(key, attempt=2))

    status, body = doors.http("router", "POST", "/v1/jev_outcome",
                              {"card": key, "attempt": 1, "outcome": "agreed"})
    assert status == 200 and body == {"card": key, "updated": 1}
    # the second outcome for the same row loses: the first write wins
    status, body = doors.http("router", "POST", "/v1/jev_outcome",
                              {"card": key, "attempt": 1, "outcome": "revised"})
    assert status == 200 and body == {"card": key, "updated": 0}

    status, shown = doors.http("router", "GET", f"/v1/show?key={key}&fmt=json")
    assert shown["jev_shadow"][0]["outcome"] == "agreed"
    assert shown["jev_shadow"][1]["outcome"] is None
    assert shown["jev_agreement"]["with_outcome"] == 1

    status, text = doors.http("router", "GET", f"/v1/show?key={key}&fmt=text")
    assert "outcome=agreed" in text


# ------------------------------------------------------------------ report
def test_jev_report_json_counts_project_filter_and_text(doors, w):
    key, = seed(w, [("A", [])])
    other, = seed(w, [("B", [])], project="other")
    router = "router"
    doors.http(router, "POST", "/v1/jev_shadow",
               rec(key, attempt=1, rule_answer="a", jev_answer="a"))     # agree
    doors.http(router, "POST", "/v1/jev_shadow",
               rec(key, attempt=2, rule_answer="a", jev_answer="b"))     # disagree
    doors.http(router, "POST", "/v1/jev_shadow",
               rec(other, kind="budget", status="timeout", jev_answer=None))

    status, report = doors.http(router, "GET", "/v1/jev_report")
    assert status == 200
    assert set(report) == {"kinds", "overall"}
    assert set(report["kinds"]) == ALL_KINDS   # every kind, even empty
    overall = report["overall"]
    assert (overall["total"], overall["ok"], overall["agree"],
            overall["disagree"], overall["timeout"]) == (3, 2, 1, 1, 1)
    assert overall["agreement_rate"] == 0.5

    status, filtered = doors.http(router, "GET", "/v1/jev_report?project=demo")
    assert status == 200
    assert (filtered["overall"]["total"], filtered["overall"]["agree"]) == (2, 1)

    status, only_other = doors.http(router, "GET", "/v1/jev_report?project=other")
    assert (only_other["overall"]["total"], only_other["overall"]["agreement_rate"]) \
        == (1, None)

    status, text = doors.http(router, "GET", "/v1/jev_report?fmt=text")
    assert status == 200 and isinstance(text, str)
    assert "harness" in text and "overall" in text and "rate 0.5000" in text


def test_jev_report_is_json_by_default_and_empty_when_no_rows(doors, w):
    status, report = doors.http("router", "GET", "/v1/jev_report")
    assert status == 200 and set(report["kinds"]) == ALL_KINDS
    assert report["overall"]["total"] == 0
    assert report["overall"]["agreement_rate"] is None


# ----------------------------------------------------------- readers/roles
def test_a_reader_may_read_the_report_but_not_write(doors, w):
    key, = seed(w, [("A", [])])
    status, report = doors.http("reader", "GET", "/v1/jev_report")
    assert status == 200 and set(report["kinds"]) == ALL_KINDS

    status, body = doors.http("reader", "POST", "/v1/jev_shadow", rec(key))
    assert (status, body["error"]) == (403, "forbidden")
    status, body = doors.http("reader", "POST", "/v1/jev_outcome",
                              {"card": key, "outcome": "agreed"})
    assert (status, body["error"]) == (403, "forbidden")
    assert shadows(w) == 0                              # nothing was written


def test_a_worker_token_is_forbidden(doors, w):
    key, = seed(w, [("A", [])])
    for verb, body in (("jev_shadow", rec(key)),
                       ("jev_outcome", {"card": key, "outcome": "x"})):
        status, body = doors.http("workerA", "POST", f"/v1/{verb}", body)
        assert (status, body["error"]) == (403, "forbidden"), verb
    assert shadows(w) == 0


def test_without_a_bearer_token_is_401(doors, w):
    assert unauth_status(doors.url + "/v1/jev_report") == 401
    assert unauth_status(doors.url + "/v1/jev_shadow",
                         json.dumps(rec("demo:A")).encode(), "POST") == 401


def test_an_l1_outside_its_project_is_403_and_inside_is_200(doors, w):
    key, = seed(w, [("A", [])])
    status, body = doors.http("l1other", "POST", "/v1/jev_shadow", rec(key))
    assert (status, body["error"]) == (403, "forbidden")
    # the same body as the l1 that owns the project is accepted
    assert doors.http("l1main", "POST", "/v1/jev_shadow", rec(key))[0] == 200
    assert doors.http("l1main", "POST", "/v1/jev_outcome",
                      {"card": key, "outcome": "agreed"}) \
        == (200, {"card": key, "updated": 1})


# ------------------------------------------------------------------ errors
def test_malformed_bodies_are_400_but_a_reader_is_403_first(doors, w):
    key, = seed(w, [("A", [])])
    status, body = doors.http("router", "POST", "/v1/jev_shadow", [1, 2])
    assert (status, body["error"]) == (400, "validation")          # not an object
    status, body = doors.http("router", "POST", "/v1/jev_shadow",
                              rec(key, bogus=1))
    assert (status, body["error"]) == (400, "validation")          # unknown field
    status, body = doors.http("router", "POST", "/v1/jev_shadow",
                              rec(key, kind="nope"))
    assert (status, body["error"]) == (400, "validation")          # bad kind
    status, body = doors.http("router", "POST", "/v1/jev_shadow", {})
    assert (status, body["error"]) == (400, "validation")          # missing card
    # the store checks the role before the body, so a bad body from a reader is 403
    status, body = doors.http("reader", "POST", "/v1/jev_shadow",
                              rec(key, bogus=1))
    assert (status, body["error"]) == (403, "forbidden")
    assert shadows(w) == 0


def test_an_unknown_card_is_404(doors, w):
    seed(w, [("A", [])])
    status, body = doors.http("router", "POST", "/v1/jev_shadow",
                              rec("demo:NOPE"))
    assert (status, body["error"]) == (404, "not_found")
    status, body = doors.http("router", "POST", "/v1/jev_outcome",
                              {"card": "demo:NOPE", "outcome": "agreed"})
    assert (status, body["error"]) == (404, "not_found")


# ------------------------------------------------------------------- audit
def test_the_jev_verbs_are_audited(doors, w):
    key, = seed(w, [("A", [])])
    doors.http("router", "POST", "/v1/jev_shadow", rec(key))
    doors.http("router", "POST", "/v1/jev_outcome",
               {"card": key, "outcome": "agreed"})
    doors.http("reader", "GET", "/v1/jev_report")
    rows = w.conn.execute(
        "SELECT action, ok FROM audit_log"
        " WHERE action IN ('jev_record', 'jev_outcome', 'jev_report')").fetchall()
    assert {(r[0], r[1]) for r in rows} == {
        ("jev_record", 1), ("jev_outcome", 1), ("jev_report", 1)}
