#!/usr/bin/env python3
"""DECISIONS.md as a view of keeper rule items (PG-EFFICIENCY-GATES slice 2, D-2746).

* ``rules DECISIONS.md --out rules.json``: parse the file into ``rule_set``
  objects (refused unless render(parse(file)) == file byte for byte); write them
  with ``plangraph-keeper rules --file rules.json`` (router).
* ``export --out decisions.json``: the keeper's ``decisions`` items (needs
  ``PG_URL`` + ``PG_TOKEN_FILE``); commit it next to DECISIONS.md.
* ``render decisions.json``: print DECISIONS.md.
* ``check decisions.json DECISIONS.md``: exit 1 when the file is not the
  render of the export (CI: DECISIONS.md is never hand-edited).
* ``tree decisions.json RULE-0-2E``: the rules a rule depends on, transitively.

Exit 0 ok, 1 check/round-trip mismatch, 2 usage or I/O error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plangraph.decisions_view import parse, render, to_rules, tree  # noqa: E402

FIELDS = ("id", "title", "description", "source", "note", "state", "depends_on")


def _load(path):
    data = json.loads(Path(path).read_text("utf-8"))
    return data["items"] if isinstance(data, dict) else data


def export_items(snapshot):
    """Rule items of a keeper snapshot, stable order, only the view's fields."""
    rows = [r for r in snapshot.get("items", []) if r.get("project") == "decisions"]
    return {"items": [{f: r.get(f) for f in FIELDS} for r in sorted(rows, key=lambda r: r["id"])]}


def _snapshot():
    url, token_file = os.environ.get("PG_URL"), os.environ.get("PG_TOKEN_FILE")
    if not url or not token_file:
        raise OSError("export needs PG_URL and PG_TOKEN_FILE")
    token = Path(token_file).read_text("utf-8").strip()
    request = urllib.request.Request(url.rstrip("/") + "/v1/snapshot",
                                     headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rules")
    r.add_argument("file")
    r.add_argument("--out", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    v = sub.add_parser("render")
    v.add_argument("export")
    c = sub.add_parser("check")
    c.add_argument("export")
    c.add_argument("file")
    t = sub.add_parser("tree")
    t.add_argument("export")
    t.add_argument("rule")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "rules":
            text = Path(args.file).read_text("utf-8")
            rules = to_rules(parse(text))
            if render(rules) != text:
                print("decisions-view: round trip differs; fix the parser before importing", file=sys.stderr)
                return 1
            Path(args.out).write_text(json.dumps({"rules": rules}, indent=1) + "\n", "utf-8")
            print(f"decisions-view: {len(rules)} items -> {args.out}")
        elif args.cmd == "export":
            Path(args.out).write_text(json.dumps(export_items(_snapshot()), indent=1) + "\n", "utf-8")
            print(f"decisions-view: export -> {args.out}")
        elif args.cmd == "render":
            sys.stdout.write(render(_load(args.export)))
        elif args.cmd == "check":
            if render(_load(args.export)) != Path(args.file).read_text("utf-8"):
                print(f"decisions-view: {args.file} is not the render of {args.export};"
                      " change rules in the keeper (rule_set), then export + render", file=sys.stderr)
                return 1
            print(f"decisions-view: {args.file} == render({args.export})")
        else:
            print("\n".join(tree(_load(args.export), args.rule)))
    except (OSError, ValueError, KeyError) as exc:
        print(f"decisions-view: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
