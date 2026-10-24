"""worker_park and worker_complete fill the Jev rows' outcome (RT-P1-JEV-SHADOW).

Both verbs call their production fill after their own commit: every still-unfilled
``jev_shadow`` row of the card gets the same ``exit_class``/``outcome`` text
:func:`plangraph.planner.shadow.record_outcome` derives for the planner, and the
first write wins, so a later verb never overwrites it.  A card with no rows opens
no transaction and files no extra audit event.  The real attempt-finish path is a
principal with role ``worker`` - the one that posts ``worker_park`` and
``worker_complete`` - which may not write ``jev_shadow`` itself, so the row rides
on the shared writer mapping (``store_jev._jev_writer``): the caller when its role
may write, the synthetic ``system`` identity otherwise, never a skipped fill and
never a raise.  Every test drives the real verb with ``PLANGRAPH_JEV`` set by
``monkeypatch.setenv`` and the client's network call replaced by the doubles in
``jev_util``; no store seam is injected.
"""
from __future__ import annotations

import pytest

from jev_util import jev_on, seed_jev_rows
from keeper_util import World, seed, to_claimed
from plangraph.keeper import auth


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def fill_audits(world):
    """Every ``jev_outcome`` audit row: each fill path files exactly one.

    Carries ``actor``/``role`` too, so a test can pin WHO filed it - the caller
    when its role may write ``jev_shadow``, else the synthetic ``system`` one.
    """
    return world.conn.execute(
        "SELECT key, ok, actor, role FROM audit_log"
        " WHERE action='jev_outcome'").fetchall()


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
    assert len(fill_audits(w)) == 16              # four routing kinds per verb


# ------------------------------------------------------- a worker-role caller
def test_a_worker_role_fills_the_outcome_on_both_verbs(w, monkeypatch):
    """The real attempt-finish path: role ``worker`` posts park and complete."""
    parked, completed = seed(w, [("A", []), ("B", [])])
    for key in (parked, completed):
        seed_jev_rows(w, key, monkeypatch)       # rows via the routing hook
        to_claimed(w, key, who="workerA")        # workerA holds the claim it parks

    w.store.worker_park(w.p("workerA"),
                        {"card": parked, "exit_class": "stall"})
    w.store.worker_complete(w.p("workerA"),
                            {"card": completed, "merged_sha": "abcdef1"})

    rows = w.store.jev_rows(parked)
    done = w.store.jev_rows(completed)
    assert rows and done
    assert [r["outcome"] for r in rows] == ["stall"] * len(rows)
    assert all(r["outcome_at"] for r in rows)
    assert [r["outcome"] for r in done] == ["complete"] * len(done)
    assert all(r["outcome_at"] for r in done)
    audits = fill_audits(w)
    assert len(audits) == 8                       # four routing kinds per verb
    # a worker may not write jev_shadow itself: the fill rides on the shared
    # writer mapping's synthetic system identity, it is never skipped
    assert {(a["actor"], a["role"]) for a in audits} == {("system", "system")}


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


# ------------------------------------ a role with no writer identity of its own
def test_a_role_that_cannot_be_mapped_still_never_raises(w, monkeypatch):
    key, = seed(w, [("A", [])])
    seed_jev_rows(w, key, monkeypatch)

    # No store verb can hand this identity to the fill - every door refuses a
    # principal with no stored row first - so the helper itself is driven: a
    # role of its own that maps to no jev_shadow writer must land the row on the
    # synthetic ``system`` identity and never raise, so the fill can never be a
    # swallowed refusal (nor a row filed under the caller).
    ghost = auth.Principal("ghost", "ghost", ["demo"], None)

    w.store._fill_jev_outcome(ghost, key, {"outcome": "complete"})

    audits = fill_audits(w)
    assert len(audits) == 4
    assert (audits[0]["actor"], audits[0]["role"]) == ("system", "system")
    rows = w.store.jev_rows(key)
    assert [r["outcome"] for r in rows] == ["complete"] * len(rows)


def test_the_verb_still_succeeds_when_the_fill_is_refused(w, monkeypatch):
    """The fill is best-effort: a refusal inside it never reaches the caller."""
    key, = seed(w, [("A", [])])
    seed_jev_rows(w, key, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("jev is down")

    monkeypatch.setattr("plangraph.planner.shadow.record_outcome", boom)
    result = w.store.worker_complete(w.p("workerA"),
                                     {"card": key, "merged_sha": "abcdef1"})

    assert result == {"card": key, "merged_sha": "abcdef1"}
    rows = w.store.jev_rows(key)
    assert rows and [r["outcome"] for r in rows] == [None] * len(rows)
    assert fill_audits(w) == []                   # the fill died before its write
