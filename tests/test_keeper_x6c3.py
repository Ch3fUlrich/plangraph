"""X6c3 — the waiver is named by the decision row, not merely recorded.

D-217 lets a router waive the second-family review on a verified ``decision``
id, but *presence* in DECISIONS.md was enough: any recorded D-id could waive
any item.  The D-row must now carry ``Waives: <project:ID>`` for the very key
being waived, as a WHOLE token (``demo:A`` does not waive ``demo:AB``), and a
token that only appears inside a code block does not count.
"""
from types import SimpleNamespace

import pytest

from keeper_util import World, seed, to_in_review
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

KEY = "demo:A"
DECISION = "D-217"


def _decisions_file(tmp_path, text):
    path = tmp_path / "DECISIONS.md"
    path.write_text(text, encoding="utf-8")
    return path


def _adapter(tmp_path, text):
    return DecisionsAdapter(_decisions_file(tmp_path, text))


def _world(tmp_path, text):
    """A seeded world whose store reads ``text`` as its decisions file."""
    world = World(tmp_path)
    seed(world, [("A", [])])
    world.store.ctx = SimpleNamespace(
        decisions=_adapter(tmp_path, text))
    return world


def _row(waiver_cell="Waives: demo:A"):
    return ("# Decisions\n\n"
            "| ID | Note | Waiver scope |\n"
            "|---|---|---|\n"
            f"| D-217 | second-family waiver | {waiver_cell} |\n")


# ------------------------------------------------------------------ adapter

def test_waives_whole_token_only(tmp_path):
    adapter = _adapter(tmp_path, _row())
    assert adapter.waives(DECISION, "demo:A") is True
    assert adapter.waives(DECISION, "demo:AB") is False
    assert adapter.waives(DECISION, "demo") is False


def test_waives_several_tokens_in_one_row(tmp_path):
    adapter = _adapter(tmp_path, _row("Waives: demo:B, demo:A"))
    assert adapter.waives(DECISION, "demo:A") is True
    adapter2 = _adapter(tmp_path, _row("Waives: demo:B | Waives: demo:A"))
    assert adapter2.waives(DECISION, "demo:A") is True
    assert adapter2.waives(DECISION, "demo:C") is False


def test_waives_ignores_code_blocks_and_unknown_ids(tmp_path):
    fenced = ("| D-200 | other | - |\n\n"
              "```markdown\n"
              "| D-217 | example | Waives: demo:A |\n"
              "```\n")
    adapter = _adapter(tmp_path, fenced)
    assert adapter.waives(DECISION, "demo:A") is False
    indented = _adapter(tmp_path, "    | D-217 | hidden | Waives: demo:A |\n")
    assert indented.waives(DECISION, "demo:A") is False
    named = _adapter(tmp_path, _row())
    assert named.waives("D-999", "demo:A") is False
    assert named.waives("D-0217", "demo:A") is True  # normalised id
    assert named.waives("d-217", "demo:A") is True
    assert named.waives("D-XX", "demo:A") is False
    assert named.waives("D-217", "  ") is False


# --------------------------------------------------------------------- verb

def test_waive_ok_when_row_names_the_key(tmp_path):
    world = _world(tmp_path, _row())
    try:
        to_in_review(world, KEY)
        result = world.store.waive_second_family(world.p("router"), KEY,
                                                 DECISION)
        assert result["review_waiver"] == DECISION
        assert world.store._item(KEY)["review_waiver"] == DECISION
        assert world.store.verify_chain() == []
    finally:
        world.close()


def test_waive_refused_when_row_has_no_token(tmp_path):
    world = _world(tmp_path, _row("-"))
    try:
        to_in_review(world, KEY)
        with pytest.raises(Refused) as exc:
            world.store.waive_second_family(world.p("router"), KEY, DECISION)
        assert exc.value.code == "evidence_unverified"
        assert exc.value.msg == f"{DECISION} does not name {KEY} (Waives:)"
        assert world.store._item(KEY)["review_waiver"] is None
        count = world.conn.execute(
            "SELECT COUNT(*) FROM events WHERE key=? AND type='waiver_set'",
            (KEY,)).fetchone()[0]
        assert count == 0
    finally:
        world.close()


def test_waive_refused_on_token_prefix(tmp_path):
    """``Waives: demo:AB`` must not satisfy the key ``demo:A``."""
    world = _world(tmp_path, _row("Waives: demo:AB"))
    try:
        to_in_review(world, KEY)
        with pytest.raises(Refused) as exc:
            world.store.waive_second_family(world.p("router"), KEY, DECISION)
        assert exc.value.code == "evidence_unverified"
        assert world.store._item(KEY)["review_waiver"] is None
    finally:
        world.close()


def test_waive_ok_with_several_tokens(tmp_path):
    world = _world(tmp_path, _row("Waives: demo:B Waives: demo:A"))
    try:
        to_in_review(world, KEY)
        assert world.store.waive_second_family(
            world.p("router"), KEY, DECISION)["review_waiver"] == DECISION
    finally:
        world.close()


def test_waive_refused_on_token_in_fenced_block(tmp_path):
    text = (_row("-") + "\n```markdown\n"
            "| D-217 | sample | Waives: demo:A |\n```\n")
    world = _world(tmp_path, text)
    try:
        to_in_review(world, KEY)
        with pytest.raises(Refused) as exc:
            world.store.waive_second_family(world.p("router"), KEY, DECISION)
        assert exc.value.code == "evidence_unverified"
    finally:
        world.close()


def test_waive_refused_without_decisions_adapter(tmp_path):
    world = _world(tmp_path, _row())
    try:
        world.store.ctx = None
        to_in_review(world, KEY)
        with pytest.raises(Refused) as exc:
            world.store.waive_second_family(world.p("router"), KEY, DECISION)
        assert exc.value.code == "evidence_unverified"
        assert world.store._item(KEY)["review_waiver"] is None
    finally:
        world.close()


def test_waive_through_the_fake_decisions_adapter(tmp_path):
    """The fake adapter exposes the same ``waives`` contract as the real one."""
    from evidence_fakes import FakeDecisions

    world = _world(tmp_path, _row())
    try:
        to_in_review(world, KEY)
        world.store.ctx = SimpleNamespace(
            decisions=FakeDecisions(present={DECISION},
                                    waiving={(DECISION, "demo:OTHER")}))
        with pytest.raises(Refused) as exc:
            world.store.waive_second_family(world.p("router"), KEY, DECISION)
        assert exc.value.code == "evidence_unverified"
        world.store.ctx = SimpleNamespace(
            decisions=FakeDecisions(present={DECISION}, waiving=None))
        assert world.store.waive_second_family(
            world.p("router"), KEY, DECISION)["review_waiver"] == DECISION
    finally:
        world.close()
