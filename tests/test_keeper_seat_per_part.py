"""KEEPER-SEAT-PER-PART (D-3084): seats cover PARTS, not items.

The family gate refuses an ACCEPT seat only when the reviewer's family is the
item's SOLE writer family -- the reviewer would then cover no part.  A family
that writes one part but not another may take an ACCEPT seat
(``set(peers) != {reviewer_family}``) and the per-part coverage gate
(``coverage.uncovered`` with ``need=2, sensitive=True``) decides what that
seat is worth.  A ``high`` round therefore accumulates seats from the second
verdict on and closes (``done``) only on the verdict that completes the
per-part coverage; a verdict that leaves coverage incomplete lands as another
seat and returns the missing writer families, and a verdict arriving once the
seats already cover (or the item is done) is refused.

The adapters are fakes, so no test touches git, the network or the filesystem.
"""
from __future__ import annotations

import pytest

from keeper_seat_helpers import (
    SHA_A,
    SHA_B,
    to_review_with_commits,
    verdict_events,
    w,  # noqa: F401 - the review world fixture
    writer,
)
from keeper_util import seed
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused


def _reviewer(w, name, family):
    """A fresh reviewer principal of *family* (the fixture has no openai seat)."""
    token = auth.add_principal(w.conn, name, "reviewer", ["demo"],
                               family=family)
    principal = auth.authenticate(w.conn, token)
    w.principals[name] = principal
    return principal


def _high_key(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",),
                  review_tier="high")
    return key


# --------------------------------------------------------------------------
# 1. mixed writers (anthropic Hand-Edit + xiaomi Writer): 1st/2nd in review,
#    3rd done -- the seat of ONE writer's family covers the OTHER's part
# --------------------------------------------------------------------------

def test_high_mixed_writers_close_on_the_third_covering_seat(w):
    """anthropic writes one part, xiaomi the other; each seat covers the other.

    The anthropic ACCEPT seat goes first although anthropic is one of the two
    writer families (D-2458's whole-item gate refused it): it covers the xiaomi
    part, and the round closes on the seat that completes the per-part
    coverage.
    """
    key = _high_key(w)
    to_review_with_commits(w, key,
                           {SHA_A: {"Hand-Edit": "D-3084"},   # anthropic
                            SHA_B: writer("mimo-v2 run1")})   # xiaomi
    openai = _reviewer(w, "rOpenai", "openai")

    first = w.store.verdict(w.p("rAnthropic1"), key, True, "anthropic seat")
    assert first["state"] == "in_review"
    assert first["reviews_passed"] == 1

    second = w.store.verdict(openai, key, True, "openai seat")
    assert second["state"] == "in_review"
    assert second["reviews_passed"] == 2
    # Vector (b): the item never closes while uncovered() is non-empty.
    assert second["missing"] == ["anthropic"]

    third = w.store.verdict(w.p("rXiaomi"), key, True, "xiaomi seat")
    assert third["state"] == "done"
    assert third["reviews_passed"] == 3
    assert len(verdict_events(w, key)) == 3


# --------------------------------------------------------------------------
# 2. sole writer family: the reviewer would cover no part
# --------------------------------------------------------------------------

def test_sole_writer_family_may_never_take_an_accept_seat(w):
    """writers only xiaomi: a xiaomi ACCEPT is refused ``same_family``."""
    key = _high_key(w)
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})  # xiaomi

    with pytest.raises(Refused) as exc:
        w.store._reviewer_family_ok(w.p("rXiaomi"), w.store._item(key))
    assert exc.value.code == "same_family"

    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), key, True, "self seat")
    assert exc.value.code == "same_family"
    assert "xiaomi" in exc.value.msg
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


# --------------------------------------------------------------------------
# 3. two OTHER families per part, at least one non-anthropic
# --------------------------------------------------------------------------

def test_repeated_seat_family_counts_once_and_is_refused(w):
    """Two anthropic seats are one family: the second seat is refused."""
    key = _high_key(w)
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})  # xiaomi

    first = w.store.verdict(w.p("rAnthropic1"), key, True, "one")
    assert first["state"] == "in_review"
    round_no = w.store._item(key)["round"]
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic2"), key, True, "two")
    assert exc.value.code == "same_family"
    item = w.store._item(key)
    assert item["state"] == "in_review"
    assert item["round"] == round_no
    assert len(verdict_events(w, key)) == 1


def test_anthropic_and_openai_seats_close_the_xiaomi_part(w):
    """anthropic+openai: two other families incl. one non-anthropic."""
    key = _high_key(w)
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})  # xiaomi
    openai = _reviewer(w, "rOpenai", "openai")

    first = w.store.verdict(w.p("rAnthropic1"), key, True, "one")
    assert first["state"] == "in_review"
    done = w.store.verdict(openai, key, True, "two")
    assert done["state"] == "done"
    assert done["reviews_passed"] == 2

    # Vector (c): a verdict arriving after coverage is complete (the item is
    # done) is refused.
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rDeepseek"), key, True, "three")
    assert exc.value.code in ("locked", "conflict")


def test_third_seat_is_refused_once_the_seats_already_cover(w):
    """A seat is taken only while coverage is still missing at that moment.

    The round below already holds two covering seats without closing (the shape
    a migrated/mixed round can leave behind): a third-or-later seat is not
    needed and is refused instead of appended.
    """
    key = _high_key(w)
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})  # xiaomi
    _reviewer(w, "rOpenai", "openai")
    item = w.store._item(key)
    for name, family in (("rAnthropic1", "anthropic"), ("rOpenai", "openai")):
        w.store._append(
            w.p(name), key, "review_verdict",
            {"reviewer": name, "family": family, "ok": True,
             "epoch": int(item["epoch"]),
             "round": int(item.get("round") or 0)},
            w.clock())

    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rDeepseek"), key, True, "not needed")
    assert exc.value.code == "conflict"
    assert w.store._item(key)["state"] == "in_review"
    assert len(verdict_events(w, key)) == 2
