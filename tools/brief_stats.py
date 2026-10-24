#!/usr/bin/env python3
"""Brief sizes over all open items (PG-BRIEF, D-708 A).

Read-only: the keeper DB is opened with ``mode=ro``, so this helper can never
write, lock or checkpoint it.  Prints the median / p90 brief text size in
chars and approx tokens (chars/4).
"""
import argparse
import sqlite3
import statistics
import sys
from math import ceil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plangraph.keeper import brief as brief_mod  # noqa: E402
from plangraph.keeper import config as config_mod  # noqa: E402


def load_open_items(db_path):
    """``(items, dep_states)`` for every open item; the DB stays read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        items = [dict(row) for row in conn.execute(
            "SELECT key,title,state,kind,owner,review_tier,evidence_kinds,"
            " priority,wait,description,done_criteria,note FROM items"
            " WHERE state='open' ORDER BY key")]
        states = {row["key"]: row["state"] for row in conn.execute(
            "SELECT key,state FROM items")}
        deps = {}
        for row in conn.execute("SELECT src,dst FROM edges"):
            deps.setdefault(row["src"], []).append(row["dst"])
    finally:
        conn.close()
    briefs = []
    for item in items:
        links = [{"key": dst, "state": states.get(dst)}
                 for dst in sorted(deps.get(item["key"], []))]
        briefs.append(brief_mod.build(item, links))
    return briefs


def percentile(sorted_sizes, pct):
    if not sorted_sizes:
        return 0
    return sorted_sizes[min(len(sorted_sizes) - 1, ceil(pct / 100 *
                         len(sorted_sizes)) - 1)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="median/p90 brief sizes")
    parser.add_argument("--keeper-config", required=True,
                        help="path to keeper.json (read for the db path only)")
    args = parser.parse_args(argv)
    cfg = config_mod.load_config(args.keeper_config)
    sizes = sorted(len(brief_mod.brief_text(b))
                   for b in load_open_items(cfg.db))
    median = int(statistics.median(sizes)) if sizes else 0
    p90 = percentile(sizes, 90)
    print(f"open items: {len(sizes)}")
    print(f"median brief: {median} chars (~{median // 4} tokens)")
    print(f"p90 brief: {p90} chars (~{p90 // 4} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
