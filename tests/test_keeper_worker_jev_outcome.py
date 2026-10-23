"""worker_park and worker_complete fill the Jev rows' outcome (RT-P1-JEV-SHADOW).

Both verbs call their production fill after their own commit: every still-unfilled
``jev_shadow`` row of the card gets the same ``exit_class``/``outcome`` text
:func:`plangraph.planner.shadow.record_outcome` derives for the planner, and the
first write wins, so a later verb never overwrites it.  A card with no rows opens
no transaction and files no extra audit event, and a principal that may not write
``jev_shadow`` is skipped while the verb itself still succeeds.  Every test drives
the real verb with ``PLANGRAPH_JEV`` set by ``monkeypatch.setenv`` and the client's
network call replaced by the doubles in ``jev_util``; no store seam is injected.
"""
from __future__ import annotations

import pytest

from jev_util import jev_on, seed_jev_rows
from keeper_util import World, seed, to_claimed


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def fill_audits(world):
    """Every ``jev_outcome`` audit row: each fill path files exactly one."""
    return world.conn.execute(
        "SELECT key, ok FROM audit_log WHERE action='jev_outcome'").fetchall()


def count_audits(world):
    return world.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


# ------------------------------------------------- fills, and first write wins
def test_both_worker_verbs_fill_the_outcome_and_the_first_write_wins(w,
                                                                    monkeypatch):
    parked, completed = seed(w, [("A", []), ("B", [])])
    for key in (parked, completed):
        seed_jev_rows(w, key, monkeypatch)       # rows via the routing hook
        to_claimed(w, key)                       # l1main holds the claim

    w.store.worker_park(w.p("l1main"),
                        {"card": parked, "exit_class": "stall"})
    w.store.worker_complete(w.p("l1main"),
                            {"card": parked, "merged_sha": "abcdef1"})
    w.store.worker_complete(w.p("l1main"),
                            {"card": completed, "merged_sha": "abcdef1"})
    w.store.worker_park(w.p("l1main"),
                        {"card": completed, "exit_class": "stall"})

    first = w.store.jev_rows(parked)
    second = w.store.jev_rows(completed)
    assert first and second
    # park filled first here, so complete's fill found nothing left to write
    assert [r["outcome"] for r in first] == ["stall"] * len(first)
    assert all(r["outcome_at"] for r in first)
    # ...and the mirror image: complete first, park cannot overwrite it
    assert [r["outcome"] for r in second] == ["complete"] * len(second)
    assert all(r["outcome_at"] for r in second)
    assert len(fill_audits(w)) == 4               # one fill audit per verb call


# ------------------------------------------------------ a card without rows
def test_a_card_without_rows_writes_nothing_and_files_no_extra_audit(w,
                                                                    monkeypatch):
    key, = seed(w, [("A", [])])
    jev_on(monkeypatch)                           # flag on, but there are no rows
    before = count_audits(w)

    result = w.store.worker_complete(w.p("l1main"),
                                     {"card": key, "merged_sha": "abcdef1"})

    assert result == {"card": key, "merged_sha": "abcdef1"}
    assert w.store.jev_rows(key) == []
    assert count_audits(w) - before == 1          # the verb's own audit row only
    assert fill_audits(w) == []


# ------------------------------------------------------- an unwritable role
def test_an_unwritable_role_is_skipped_while_the_verb_still_succeeds(w,
                                                                     monkeypatch):
    key, = seed(w, [("A", [])])
    seed_jev_rows(w, key, monkeypatch)

    # workerA may act on the card but may not write jev_shadow rows
    result = w.store.worker_complete(w.p("workerA"),
                                     {"card": key, "merged_sha": "abcdef1"})

    rows = w.store.jev_rows(key)
    assert result["card"] == key and rows
    assert [r["outcome"] for r in rows] == [None] * len(rows)
    assert fill_audits(w) == []                   # skipped before any transaction

    w.store.worker_complete(w.p("l1main"),
                            {"card": key, "merged_sha": "abcdef1"})
    assert [r["outcome"] for r in w.store.jev_rows(key)] == ["complete"] * len(rows)
    assert len(fill_audits(w)) == 1
