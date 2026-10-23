"""shadow.record_outcome fills the keeper's Jev rows (RT-P1-JEV-SHADOW).

The keeper branch of the production entry point: given ``store``, ``principal``
and ``card_key`` it fills the still-unfilled ``jev_shadow`` rows of that card
through the real ``store.jev_outcome`` door - every attempt when ``attempt`` is
omitted, only the named attempt (``None`` matching the unnumbered rows) when it
is given - and reports whether anything landed.  ``PLANGRAPH_JEV`` is set by
``monkeypatch.setenv`` and the network call is replaced by the double in
``jev_util``; no store seam is injected into ``record_outcome`` anywhere.
"""
from __future__ import annotations

import pytest

from jev_util import seed_jev_rows
from keeper_util import World, seed
from plangraph.planner import shadow as shadow_mod


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


_MISSING = object()


def fill(w, outcome, key, attempt=_MISSING):
    """Call the production entry point with the keeper branch wired in."""
    kw = {} if attempt is _MISSING else {"attempt": attempt}
    return shadow_mod.record_outcome(None, outcome, store=w.store,
                                     principal=w.p("l1main"), card_key=key, **kw)


def test_without_an_attempt_every_row_of_the_card_is_filled(w, monkeypatch):
    key, = seed(w, [("A", [])])
    seed_jev_rows(w, key, monkeypatch, attempt=1)
    seed_jev_rows(w, key, monkeypatch, attempt=2)
    rows = w.store.jev_rows(key)
    assert {r["attempt"] for r in rows} == {1, 2}
    assert all(r["outcome"] is None for r in rows)

    landed = fill(w, {"outcome": "complete"}, key)

    assert landed is True
    assert [r["outcome"] for r in w.store.jev_rows(key)] == ["complete"] * len(rows)
    assert all(r["outcome_at"] for r in w.store.jev_rows(key))


def test_with_an_attempt_only_that_attempt_is_filled(w, monkeypatch):
    numbered, empty = seed(w, [("A", []), ("B", [])])
    seed_jev_rows(w, numbered, monkeypatch, attempt=1)
    seed_jev_rows(w, numbered, monkeypatch, attempt=2)

    landed = fill(w, {"exit_class": "stall"}, numbered, attempt=1)

    assert landed is True
    after = w.store.jev_rows(numbered)
    first = [r for r in after if r["attempt"] == 1]
    assert [r["outcome"] for r in first] == ["stall"] * len(first)
    assert all(r["outcome"] is None for r in after if r["attempt"] == 2)
    # first write wins: the same attempt again lands nothing
    assert fill(w, {"exit_class": "stall"}, numbered, attempt=1) is False
    # a card that never ran Jev has nothing to fill either
    assert fill(w, {"outcome": "complete"}, empty) is False
    assert w.store.jev_rows(empty) == []


def test_attempt_none_matches_only_the_unnumbered_rows(w, monkeypatch):
    key, = seed(w, [("A", [])])
    seed_jev_rows(w, key, monkeypatch, attempt=3)

    assert fill(w, {"outcome": "complete"}, key, attempt=None) is False
    assert all(r["outcome"] is None for r in w.store.jev_rows(key))
