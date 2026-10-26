"""PG-FOLD-UNKNOWN-TYPE: an event type this build cannot replay is never silently skipped.

Mutations killed: ``fold`` dropping a ``kind`` outside ``EVENT_TYPES`` without a
trace (the cache drifts, or an old rebuild later undoes the newer keeper's
event), ``verify_integrity`` staying green over such an event, a *known* type
being reported as unknown, ``fold``'s old single-argument call style breaking,
and ``pg admin verify`` hiding the type/seq of the unknown event.

The synthetic event goes in through ``store._append`` -- the same hash-chained
append path the store uses -- so ``verify_chain`` must stay green throughout.
"""
from __future__ import annotations

import ast
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from keeper_util import World, seed, to_in_review
from plangraph.keeper import cli_admin
from plangraph.keeper import events as events_mod
from plangraph.keeper.events import fold, read_events, verify_chain, verify_integrity
from plangraph.keeper.schema import EVENT_TYPES

UNKNOWN = "zz_future_event"      # a type only a newer keeper knows

#: in ``EVENT_TYPES`` but with no branch in ``fold``: replayed by
#: ``token_fold``/the store instead, so a known no-op, never "unknown".
BRANCHLESS = {"review_verdict", "token_minted", "outage_recorded", "item_logged"}

#: every field any branch may read, so a known type replays without a KeyError.
RICH = {"epoch": 1, "claimed_by": "l1main", "lease_until": "2030-01-01T00:00:00Z",
        "state": "claimed", "round": 1, "priority": 2, "review_tier": "standard",
        "kind": "lane", "wait": None, "operator": False, "decision_id": "D-1",
        "run_id": "run-1", "seats": [], "worker": "workerA"}

COLUMNS = ("state", "epoch", "round", "claimed_by", "delegate", "lease_until",
           "reviewer", "review_waiver", "priority", "run_ids")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _row(world, key):
    return tuple(world.conn.execute(
        f"SELECT {','.join(COLUMNS)} FROM items WHERE key=?", (key,)).fetchone())


def _unknown(world):
    return [p for p in verify_integrity(world.conn)
            if p["code"] == "unknown_event_type"]


# --------------------------------------------------------------- the headline
def test_unknown_type_is_reported_once_and_the_item_state_is_untouched(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_in_review(w, key)                     # normal verbs first
    assert verify_chain(w.conn) == [] and verify_integrity(w.conn) == []

    before_fold = fold(read_events(w.conn))
    before_row = _row(w, key)

    # the same hash-chained append path the store uses
    seq, digest = w.store._append(w.p("router"), key, UNKNOWN,
                                  {"note": "written by a newer keeper"}, w.clock())

    assert w.conn.execute("SELECT hash FROM events WHERE seq=?",
                          (seq,)).fetchone()[0] == digest
    assert verify_chain(w.conn) == []                # the chain is still valid

    problems = verify_integrity(w.conn)
    assert [p for p in problems if p["code"] != "unknown_event_type"] == []
    assert [p for p in problems if p["code"] == "unknown_event_type"] == [
        {"key": key, "code": "unknown_event_type", "field": "type",
         "expected": None, "got": UNKNOWN, "seq": seq}]
    # the "right" seq: the seq of that very event as the log replays it
    assert seq == [e["seq"] for e in read_events(w.conn)
                   if e["type"] == UNKNOWN][-1]

    after_fold = fold(read_events(w.conn))
    assert after_fold == before_fold                 # no state, no new key
    assert after_fold[key]["state"] == "in_review" and after_fold[key]["epoch"] == epoch
    assert _row(w, key) == before_row                # and no cache row moved


def test_unknown_type_without_a_key_reports_a_none_key(w):
    (key,) = seed(w, [("A", [])])
    seq, _ = w.store._append(w.p("router"), None, UNKNOWN, {}, w.clock())
    assert verify_chain(w.conn) == []
    assert _unknown(w) == [{"key": None, "code": "unknown_event_type",
                            "field": "type", "expected": None,
                            "got": UNKNOWN, "seq": seq}]


def test_admin_verify_line_names_the_type_and_seq(w, capsys):
    (key,) = seed(w, [("A", [])])
    seq, _ = w.store._append(w.p("router"), key, UNKNOWN, {}, w.clock())
    cfg = SimpleNamespace(db=str(w.path), db_options={})
    assert cli_admin._cmd_verify(Namespace(), cfg) == 2
    out = capsys.readouterr().out
    assert f"{key} unknown_event_type type={UNKNOWN} seq={seq}" in out
    # a problem with a None key still prints a readable line
    assert f"- unknown_event_type type={UNKNOWN} seq={seq}" in cli_admin._problem_line(
        {"key": None, "code": "unknown_event_type", "field": "type",
         "expected": None, "got": UNKNOWN, "seq": seq})
    # every other problem keeps its dict repr (the contract greps codes from it)
    assert "cache_mismatch" in cli_admin._problem_line(
        {"key": key, "code": "cache_mismatch", "field": "state",
         "expected": "open", "got": "claimed"})


# ------------------------------------------------- a clean chain stays clean
def test_known_types_only_reports_no_unknown_problems(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    assert _unknown(w) == []
    assert verify_integrity(w.conn) == []            # not even another code


def _fold_branch_kinds():
    """The ``kind`` literals the ``fold`` if/elif chain actually compares."""
    tree = ast.parse(Path(events_mod.__file__).read_text(encoding="utf-8"))
    fold_node = next(n for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == "fold")
    kinds = set()
    for node in ast.walk(fold_node):
        if not (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name) and node.left.id == "kind"):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                kinds.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List)):
                kinds.update(elt.value for elt in comparator.elts
                             if isinstance(elt, ast.Constant))
    return kinds


def test_every_event_type_is_branched_or_intentionally_ignored():
    """PG-FOLD-UNKNOWN-TYPE audit: no EVENT_TYPES member is *neither*."""
    branched = _fold_branch_kinds()
    # every branch handles a type this build declares (no dead/drifted branch)
    assert branched <= set(EVENT_TYPES)
    # ... and every declared type either has a branch or is a documented no-op
    assert set(EVENT_TYPES) - branched == BRANCHLESS


@pytest.mark.parametrize("kind", EVENT_TYPES)
def test_every_known_type_appends_without_an_unknown_problem(w, kind):
    """DB level: each EVENT_TYPES member folds as a known no-op/op, never as unknown."""
    key = f"demo:F_{kind}"
    w.store._append(w.p("router"), key, kind, RICH, w.clock())
    assert verify_chain(w.conn) == []
    assert _unknown(w) == []


@pytest.mark.parametrize("kind", EVENT_TYPES)
def test_every_known_type_folds_without_being_reported(kind):
    """Fold level: the side channel stays empty for every type this build knows."""
    seen = []
    state = fold([{"seq": 7, "type": kind, "key": "demo:A", "payload": RICH}],
                 unknown=seen)
    assert seen == []
    assert "demo:A" in state                          # folded as a known type


# ------------------------------------------------------- backward compatibility
def test_fold_keeps_the_old_single_argument_call_style():
    events_ = [{"seq": 1, "type": "item_created", "key": "p:X", "actor": "r",
                "payload": {"priority": 3}},
               {"seq": 2, "type": UNKNOWN, "key": "p:X", "actor": "r",
                "payload": {"from": "future"}}]
    old_style = fold(events_)                         # exactly what every caller does
    assert old_style["p:X"]["state"] == "open" and old_style["p:X"]["priority"] == 3
    seen = []
    assert fold(events_, unknown=seen) == old_style   # same bytes, side channel filled
    assert seen == [{"seq": 2, "type": UNKNOWN, "key": "p:X"}]
    # an unknown type never creates an item entry of its own
    assert fold([{"seq": 1, "type": UNKNOWN, "key": "p:Y", "payload": {}}]) == {}
