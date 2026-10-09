"""FU-X7-LOWS: the code-level lows from docs/reviews/2026-10-02-keeper-x6c-x7-seats.md.

* ``X-Waives:`` / ``Pre-closes:`` are other words, not the Waives:/Closes: keywords.
* ``verify_integrity`` compares each evidence row's (epoch, kind, verified), not only counts.
* Doc test: an L1 may raise ``none`` to ``standard`` but may not lower ``standard`` to ``none``.
"""
from __future__ import annotations

import sqlite3

import pytest

from keeper_util import World, defn, to_in_review
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

HEAD = "| ID | Area | Decision | By | Evidence |\n|---|---|---|---|---|\n"


def _log(tmp_path, *rows):
    path = tmp_path / "decisions-log.md"
    path.write_text(HEAD + "".join(rows), encoding="utf-8")
    return DecisionsAdapter(path)


def test_prefixed_words_are_not_keywords(tmp_path):
    a = _log(tmp_path,
             "| D-1 | x | X-Waives: demo:A and Pre-closes: demo:B | router | e |\n",
             "| D-2 | x | ok. Closes: demo:C Waives: demo:D | router | e |\n",
             "| D-3 | x | under_closes: demo:E | router | e |\n")
    assert not a.has_closes_keyword("D-1") and a.closes("D-1") == set()
    assert not a.waives("D-1", "demo:A")
    assert a.closes("D-2") == {"demo:C"} and a.waives("D-2", "demo:D")
    assert not a.has_closes_keyword("D-3")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("A"), defn("G", ["A"], kind="goal")])
    yield world
    world.close()


def test_verify_flags_a_relabelled_evidence_kind(w):
    to_in_review(w, "demo:A", kind="decision")
    assert w.store.verify_integrity() == []
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE evidence SET kind='run-record' WHERE key='demo:A'")
    raw.commit()
    raw.close()
    probs = w.store.verify_integrity()
    assert [(p["key"], p["field"]) for p in probs] == [("demo:A", "evidence_shape")]


def test_verify_flags_evidence_moved_to_another_epoch(w):
    to_in_review(w, "demo:A", kind="decision")
    raw = sqlite3.connect(str(w.path))
    raw.execute("UPDATE evidence SET epoch=epoch+7 WHERE key='demo:A'")
    raw.commit()
    raw.close()
    assert [p["field"] for p in w.store.verify_integrity()] == ["evidence_shape"]


def test_l1_may_raise_none_to_standard_but_not_lower(tmp_path):
    world = World(tmp_path)
    try:
        r, l1 = world.p("router"), world.p("l1main")
        world.store.create_items(r, [
            defn("N", review_tier="none", evidence_kinds=["decision"]),
            defn("S"), defn("G", ["N", "S"], kind="goal")])
        world.store.edit_definition(l1, "demo:N", {"review_tier": "standard",
                                                   "evidence_kinds": ["any"]})
        assert world.store.show(r, "demo:N")["review_tier"] == "standard"
        with pytest.raises(Refused) as exc:
            world.store.edit_definition(l1, "demo:S", {"review_tier": "none",
                                                       "evidence_kinds": ["decision"]})
        assert exc.value.code == "forbidden"
        assert world.store.show(r, "demo:S")["review_tier"] == "standard"
    finally:
        world.close()
