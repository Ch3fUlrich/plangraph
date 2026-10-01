#!/usr/bin/env python3
"""Validate the plan-v3 import draft in data/plan-v3/*.yaml.

Read-only. Uses only the stdlib plus PyYAML and graphlib (docs/SPEC.md).
Exit 0 when the draft is clean, 1 when any problem is found.

Checks:
  * duplicate keys
  * missing required fields / bad enum values
  * title / done_criteria length limits
  * unknown dependency keys (bare ids are normalised to <project>:<id>)
  * self dependencies
  * cycles (via graphlib)
  * non-terminal items that cannot reach routing:GOAL
"""
from __future__ import annotations

import sys
from pathlib import Path

import graphlib
import yaml

ROOT = Path(__file__).resolve().parent.parent
IMPORT_DIR = ROOT / "data" / "plan-v3"
GLOB = "*.yaml"
GOAL_KEY = "routing:GOAL"
TERMINAL = {"done", "cancelled"}
VALID_KINDS = {"lane", "gate", "goal"}
VALID_STATES = {"open", "claimed", "in_review", "done", "cancelled"}
VALID_EVIDENCE = {"commit+ci", "run-record", "semaphore-task", "decision", "any"}
REQUIRED = [
    "project", "id", "title", "kind", "track", "owner", "state",
    "estimate_h", "depends_on", "corrects", "done_criteria",
    "evidence_kinds", "operator", "priority", "source",
]
TITLE_MAX = 100
DONE_MAX = 300


def load_items():
    items = []
    for path in sorted(IMPORT_DIR.glob(GLOB)):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "items" not in data:
            raise SystemExit(f"{path}: expected a mapping with an 'items' list")
        if not isinstance(data["items"], list):
            raise SystemExit(f"{path}: 'items' must be a list")
        for raw in data["items"]:
            if not isinstance(raw, dict):
                raise SystemExit(f"{path}: every item must be a mapping")
            raw["_file"] = path.name
            items.append(raw)
    return items


def main() -> int:
    if not IMPORT_DIR.is_dir():
        print(f"FAIL no import directory: {IMPORT_DIR}")
        return 1
    items = load_items()
    problems = []

    # --- normalise to keys and check required fields ---
    index = {}
    order = []
    for it in items:
        where = f"{it.get('_file')}#{it.get('id', '?')}"
        missing = [f for f in REQUIRED if f not in it]
        if missing:
            problems.append(f"missing-fields {where}: {', '.join(missing)}")
            continue
        key = f"{it['project']}:{it['id']}"
        if key in index:
            problems.append(f"dup_key {key} (also in {index[key]['_file']})")
            continue
        index[key] = it
        order.append(key)

    # --- resolve dependencies, enums, lengths ---
    deps = {}
    for key in order:
        it = index[key]
        project = it["project"]
        resolved = []
        for dep in it["depends_on"]:
            resolved.append(dep if ":" in dep else f"{project}:{dep}")
        deps[key] = resolved

        if it["kind"] not in VALID_KINDS:
            problems.append(f"bad-kind {key}: {it['kind']}")
        if it["state"] not in VALID_STATES:
            problems.append(f"bad_state {key}: {it['state']}")
        if not isinstance(it["done_criteria"], str) or len(it["done_criteria"]) > DONE_MAX:
            problems.append(f"long-done_criteria {key}: {len(str(it['done_criteria']))} chars")
        if not isinstance(it["title"], str) or len(it["title"]) > TITLE_MAX:
            problems.append(f"long-title {key}: {len(str(it['title']))} chars")
        ev = it["evidence_kinds"]
        if not isinstance(ev, list) or not ev:
            problems.append(f"bad-evidence {key}: {ev!r}")
        elif ev != ["any"] and any(e not in VALID_EVIDENCE for e in ev):
            bad = [e for e in ev if e not in VALID_EVIDENCE]
            problems.append(f"bad-evidence {key}: {bad}")
        if key in resolved:
            problems.append(f"self_dep {key}")

    # --- unknown deps ---
    for key in order:
        for dep in deps[key]:
            if dep not in index:
                problems.append(f"unknown_dep {key} -> {dep}")

    # --- cycles (edges: item -> its prerequisites) ---
    known = {k: [d for d in deps[k] if d in index] for k in order}
    try:
        graphlib.TopologicalSorter(known).prepare()
    except graphlib.CycleError as exc:
        cycle = " -> ".join(exc.args[1])
        problems.append(f"cycle: {cycle}")

    # --- reachability: non-terminal items must reach GOAL via dependents ---
    if GOAL_KEY not in index:
        problems.append(f"no_goal: {GOAL_KEY} missing")
    else:
        successors = {k: [] for k in order}
        for key in order:
            for dep in known[key]:
                successors[dep].append(key)
        for key in order:
            if index[key]["state"] in TERMINAL:
                continue
            seen = set()
            stack = [key]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(successors[cur])
            if GOAL_KEY not in seen:
                problems.append(f"unreachable_goal {key}")

    # --- report ---
    by_project = {}
    by_track = {}
    for it in items:
        by_project[it.get("project", "?")] = by_project.get(it.get("project", "?"), 0) + 1
        by_track[it.get("track", "?")] = by_track.get(it.get("track", "?"), 0) + 1
    print(f"loaded {len(items)} items from {IMPORT_DIR}")
    print("by project: " + ", ".join(f"{k}={by_project[k]}" for k in sorted(by_project)))
    print("by track:   " + ", ".join(f"{k}={by_track[k]}" for k in sorted(by_track)))
    if not problems:
        print("OK: no duplicate keys, unknown deps, cycles, missed fields or unreachable items")
        return 0
    print(f"FAIL: {len(problems)} problem(s)")
    for p in sorted(set(problems)):
        print("  " + p)
    return 1


if __name__ == "__main__":
    sys.exit(main())
