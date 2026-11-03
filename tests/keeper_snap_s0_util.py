"""Deterministic snapshot fixture for the KEEPER-SNAP-S0-TIMINGS tests.

One small world whose unfiltered snapshot exercises the ``decision_text`` edge
cases (two spellings of one id, ids with no row, an empty DECISION cell, a
fenced decoy row, a >600-char cell) plus sentinel names for the leak test;
``body`` serialises like ``snapshot_io`` so the pre-change golden compares
byte for byte.
"""
from __future__ import annotations

import json

from keeper_util import World, pass_review, seed, to_claimed
from plangraph.keeper.evidence_decisions import DecisionsAdapter

#: item key, decision id and decision text the timing line must never carry.
SENTINELS = ("demo:SENTRYKEY", "D-987654", "sentinel decision text")
LONG = "long cell " * 80                                   # 800 chars > the 600-char cap

DECISIONS_DOC = (
    "| D-209 | demo | sentinel decision text |\n"
    "| D-210 | demo |  |\n"
    "| D-210 | demo | second row text for 210 |\n"
    f"| D-211 | demo | {LONG}|\n"
    "| D-500 | demo |  |\n"
    "```\n"
    "| D-999 | demo | fenced decoy text |\n"
    "```\n"
    "| D-987654 | demo | sentinel decision text |\n"
)

#: raw cited id -> expected ``decision_text`` entry (nothing else may appear).
EXPECTED_TEXT = {
    "D-0209": "sentinel decision text",
    "d-209": "sentinel decision text",
    "D-210": "second row text for 210",
    "D-211": LONG.strip()[:600],
    "D-987654": "sentinel decision text",
}


def build(tmp_path):
    """(world, adapter): lanes citing decision ids, one decisions file."""
    path = tmp_path / "decisions.md"
    path.write_text(DECISIONS_DOC, encoding="utf-8")
    adapter = DecisionsAdapter(path)
    w = World(tmp_path)
    w.store.ctx = {"decisions": adapter}        # the startup adapter (evidence.py)
    seed(w, [("A", []), ("B", []), ("SENTRYKEY", [])])
    epoch = to_claimed(w, "demo:A")
    for did in ("D-0209", "d-209", "D-404", "x1", "D-500", "D-987654"):
        w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "decision", {"id": did})
    epoch = to_claimed(w, "demo:B")
    w.store.add_evidence(w.p("l1main"), "demo:B", epoch, "decision", {"id": "D-210"})
    w.store.add_evidence(w.p("l1main"), "demo:B", epoch, "decision", {"id": "D-211"})
    w.store.submit(w.p("l1main"), "demo:B", epoch)
    pass_review(w, "demo:B")
    epoch = to_claimed(w, "demo:SENTRYKEY")
    w.store.add_evidence(w.p("l1main"), "demo:SENTRYKEY", epoch, "decision",
                         {"id": "D-987654"})
    return w, adapter


def body(w):
    """The unfiltered snapshot body, serialised as ``snapshot_io`` writes it."""
    return json.dumps(w.store.snapshot(w.p("router"))).encode("utf-8")
