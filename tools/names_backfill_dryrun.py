#!/usr/bin/env python3
"""MODEL-NAMES: read-only dry run of the item-name / description backfill.

LIVE backfill is a live step: this script only lists.

Input is a plan snapshot JSON exactly as keeper writes it (a mapping with an
``items`` list; ``tests/fixtures/mini_snapshot.json`` has the shape).  For every
active item that ``plangraph.views.warnings`` flags, one line goes to stdout,
sorted exactly as the warnings are (by code, then key):

    KEY | code | current title | proposed title | description: <head or MISSING>

* the proposed title derives from the key: split on ``-`` and ``_``, lowercase,
  capitalise each word, join with spaces;
* ``description:`` is the first 80 chars of the note's folded ``description:``
  line (what ``migrate_descriptions`` would move) with its continuation lines
  flattened, or ``MISSING`` when the note carries no folded description;
* the last line is ``count: <N>``, the number of listing lines.

``--json`` prints the same list as a JSON array of the same rows.

Read-only by construction: no database is opened, no write verb is imported,
no network call is made, and only the key/title/note head is ever printed.
Exit 0 for every readable input, 2 when the input cannot be read.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plangraph import views  # noqa: E402

#: Same first-line rule as ``plangraph.keeper.store_names`` (read-only mirror,
#: deliberately not imported: that module carries the live write verb).
_FIELD_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*: ")
_PREFIX = "description: "
#: Characters of the folded description ever printed (never the whole note).
HEAD = 80
SPLIT = re.compile(r"[-_]")


def proposed_title(key):
    """Words from *key*: split on ``-``/``_``, lowercase, capitalise each."""
    words = [w for w in SPLIT.split(key or "") if w]
    return " ".join(w.lower().capitalize() for w in words)


def folded_description(note):
    """The note's leading ``description:`` block flattened to one line, else None."""
    if not isinstance(note, str) or not note.startswith(_PREFIX):
        return None
    lines = note.split("\n")
    taken = [lines[0][len(_PREFIX):]]
    rest = 1
    while rest < len(lines) and not _FIELD_LINE.match(lines[rest]):
        taken.append(lines[rest])
        rest += 1
    return " ".join(taken)


def description_head(note):
    """First 80 chars of the folded description, or ``MISSING``."""
    folded = folded_description(note)
    if not folded or not folded.strip():
        return "MISSING"
    return folded[:HEAD]


def rows_for(items):
    """One dict per ``views.warnings`` row, in the warnings' own order."""
    index = views.index(items)
    rows = []
    for problem in views.warnings(items):
        item = index.get(problem["key"]) or {}
        rows.append({"key": problem["key"], "code": problem["code"],
                     "title": item.get("title") or "",
                     "proposed": proposed_title(problem["key"]),
                     "description": description_head(item.get("note"))})
    return rows


def line_of(row):
    return (f"{row['key']} | {row['code']} | {row['title']} | "
            f"{row['proposed']} | description: {row['description']}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="names_backfill_dryrun",
        description="list name/description backfill candidates from a snapshot (read-only)")
    parser.add_argument("snapshot", help="plan snapshot JSON as keeper writes it")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the same list as JSON")
    args = parser.parse_args(argv)
    try:
        data = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return 2
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return 2
    items = [it for it in data["items"] if isinstance(it, dict) and "key" in it]
    rows = rows_for(items)
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for row in rows:
        print(line_of(row))
    print(f"count: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
