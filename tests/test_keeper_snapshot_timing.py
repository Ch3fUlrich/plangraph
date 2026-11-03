"""KEEPER-SNAP-S0-TIMINGS: stderr timing lines carry ms and counts only.

Every line is ``keeper: <name>_timing`` plus ``name=<non-negative int>`` fields:
never a decision id, decision text or an item key, whatever the fixture cites.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

from keeper_snap_s0_util import SENTINELS, build
from keeper_util import World, seed, to_in_review
from plangraph.keeper import anchor

ROOT = Path(__file__).resolve().parents[1]
LINE_RE = re.compile(r"^keeper: ([a-z_]+_timing)((?: [a-z_]+=[0-9]+)+)$")


# (e) the timing line contains no item key, decision id or decision text
def test_snapshot_timing_line_carries_counts_and_no_secrets(tmp_path, capsys):
    w, _ = build(tmp_path)
    w.store.snapshot(w.p("router"))
    lines = [line for line in capsys.readouterr().err.splitlines()
             if line.startswith("keeper: ")]
    assert len(lines) == 1                       # one line per snapshot build
    assert lines[0].startswith("keeper: snapshot_timing ")
    match = LINE_RE.match(lines[0])
    assert match, lines[0]
    fields = dict(part.split("=", 1) for part in match.group(2).split())
    for name in ("total_ms", "items_ms", "edges_ms", "meta_ms", "answers_ms",
                 "evidence_ms", "verdicts_ms", "shape_ms", "aliases_ms",
                 "activity_ms", "decision_ms", "progress_ms", "serialise_ms",
                 "items", "decision_ids", "bytes"):
        assert name in fields, name
    assert fields["items"] == "4"
    assert fields["decision_ids"] == "8"
    assert int(fields["bytes"]) > 0
    assert not any(sentinel in lines[0] for sentinel in SENTINELS + ("demo",))


def test_anchor_timing_line_for_a_tick_that_did_work(tmp_path):
    w = World(tmp_path)
    (key,) = seed(w, [("A", [])])
    err = io.StringIO()
    anchorer = anchor.Anchorer({"anchor_file": str(tmp_path / "anchor.log")},
                               clock=lambda: 1000.0, err=err)
    anchorer.record(w.conn, "start")
    err.truncate(0)
    err.seek(0)
    to_in_review(w, key)
    w.store.verdict(w.p("reviewer"), key, True, "ok")
    assert anchorer.tick(w.conn) == "ok"          # a done transition is due
    lines = err.getvalue().splitlines()
    assert len(lines) == 1 and lines[0].startswith("keeper: anchor_timing ")
    match = LINE_RE.match(lines[0])
    assert match, lines[0]
    fields = dict(part.split("=", 1) for part in match.group(2).split())
    assert fields["ok"] == "1" and int(fields["total_ms"]) >= 0
    assert not any(sentinel in lines[0] for sentinel in SENTINELS + ("demo",))
    assert anchorer.tick(w.conn) is None          # nothing new: no line
    assert len(err.getvalue().splitlines()) == 1


# (f) the bench runs with seeded rows and prints the per-phase split
def test_bench_seeds_rows_and_prints_the_phase_split(capsys):
    sys.path.insert(0, str(ROOT / "tools"))
    import bench_keeper_snapshot as bench
    bench.main(["--backend", "sqlite", "--sizes", "12", "--writes", "1",
                "--reads", "1", "--decisions", "5"])
    out = capsys.readouterr().out
    split = [line for line in out.splitlines() if line.startswith("split 12:")]
    assert split, out
    for name in ("items=", "decision_ids=5", "bytes=", "items_ms=",
                 "evidence_ms=", "decision_ms=", "progress_ms=", "serialise_ms="):
        assert name in split[0], name
