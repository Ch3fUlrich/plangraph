"""The keeper's verdict shadows ONE qc_accept row (RT-P1-JEV-SHADOW).

``store.verdict`` files one observe-only ``qc_accept`` row for the verdict it
just landed - ``accept`` for a pass, ``reject`` for a change request - through
its production ``qc_with_shadow`` call site, after the verdict's commit.  The
tests drive the real verb with ``PLANGRAPH_JEV`` set by ``monkeypatch.setenv``
and the client's network call replaced by the doubles in ``jev_util``; no store
seam is injected anywhere, and a disabled, slow or broken Jev must leave the
verdict exactly as it was.
"""
from __future__ import annotations

import time

import pytest

from jev_util import ask_recorder, fixed_ask, jev_on, slow_ask, unavailable_ask
from keeper_util import World, seed, to_in_review
from plangraph.keeper import fmt
from plangraph.keeper.errors import Refused
from plangraph.planner import jev_client


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def review_cards(w, *names):
    """Seeded cards sitting ``in_review``, created in one batch (one goal)."""
    keys = seed(w, [(name, []) for name in names])
    for key in keys:
        to_in_review(w, key)
    return keys


# --------------------------------------------------------- rows land and show
def test_a_pass_and_a_change_request_each_land_one_qc_accept_row(w, monkeypatch):
    accepted, changes = review_cards(w, "A", "B")
    router = w.p("router")

    jev_on(monkeypatch, fixed_ask("accept"))
    passed = w.store.verdict(w.p("reviewer"), accepted, True, "clean review")
    from plangraph.planner.jev_shadow import drain
    drain()
    jev_on(monkeypatch, fixed_ask("reject"))
    asked = w.store.verdict(w.p("reviewer"), changes, False, "racy flush")
    drain()

    # the verdict results are the plain rules results, byte for byte
    assert passed == {"key": accepted, "state": "done", "reviews_passed": 1}
    assert asked == {"key": changes, "state": "claimed", "reviews_passed": 0}

    for key, word in ((accepted, "accept"), (changes, "reject")):
        row, = w.store.jev_rows(key)             # exactly ONE row per verdict
        assert (row["kind"], row["rule_answer"]) == ("qc_accept", word)
        assert row["status"] == "ok" and row["attempt"] is None
        assert row["outcome"] == word and row["decision_id"]
        info = w.store.show(router, key)         # the show door
        assert [r["kind"] for r in info["jev_shadow"]] == ["qc_accept"]
        assert [r["rule_answer"] for r in info["jev_shadow"]] == [word]
        assert info["jev_agreement"]["total"] == 1

    report = w.store.jev_report(router)          # the report door
    assert (report["kinds"]["qc_accept"]["total"],
            report["kinds"]["qc_accept"]["ok"]) == (2, 2)
    assert "qc_accept" in fmt.jev_report_text(report)
    assert "jev qc_accept" in fmt.show_text(w.store.show(router, accepted))


# --------------------------------------------------------------------- off
def test_with_the_flag_unset_there_is_no_ask_and_no_row(w, monkeypatch):
    key, = review_cards(w, "A")
    calls, ask = ask_recorder()
    monkeypatch.setattr(jev_client, "ask", ask)
    monkeypatch.delenv("PLANGRAPH_JEV", raising=False)

    result = w.store.verdict(w.p("reviewer"), key, True, "clean review")

    assert result["state"] == "done"
    assert calls == []
    assert w.store.jev_rows(key) == []
    qc = w.store.jev_report(w.p("router"))["kinds"]["qc_accept"]
    assert (qc["total"], qc["ok"]) == (0, 0)


# --------------------------------------------------------- a Jev that fails
@pytest.mark.parametrize("ask,status", [(slow_ask, "timeout"),
                                        (unavailable_ask, "unavailable")],
                         ids=["timeout", "unavailable"])
def test_a_failing_jev_never_changes_the_verdict_result(w, monkeypatch,
                                                        ask, status):
    passing, changing = review_cards(w, "A", "B")
    jev_on(monkeypatch, ask)
    monkeypatch.setenv("PLANGRAPH_JEV_TIMEOUT_S", "0.2")

    started = time.monotonic()
    passed = w.store.verdict(w.p("reviewer"), passing, True, "clean review")
    asked = w.store.verdict(w.p("reviewer"), changing, False, "racy flush")
    elapsed = time.monotonic() - started
    from plangraph.planner.jev_shadow import drain
    drain()

    assert passed == {"key": passing, "state": "done", "reviews_passed": 1}
    assert asked == {"key": changing, "state": "claimed", "reviews_passed": 0}
    assert elapsed < 3.0                       # bounded by the guarded ask
    rows = w.store.jev_rows(passing) + w.store.jev_rows(changing)
    assert [r["status"] for r in rows] == [status, status]
    assert all(r["jev_answer"] is None for r in rows)


# ----------------------------------------------------------------- refusals
def test_a_refused_verdict_files_nothing_at_all(w, monkeypatch):
    key, = seed(w, [("A", [])])                # never submitted: not in review
    jev_on(monkeypatch)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), key, True, "clean review")
    assert exc.value.code == "bad_state"
    assert w.store.jev_rows(key) == []
