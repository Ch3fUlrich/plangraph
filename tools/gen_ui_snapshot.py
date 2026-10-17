#!/usr/bin/env python
"""Generate tests/fixtures/ui_snapshot.json (the fixed browser-test snapshot).

Deterministic (no randomness, no clock) fixed snapshot for the browser-test
infrastructure (UI-BROWSER-TESTS W1). Derived from the item schema of
examples/snapshot.json; all names are fictional. Covers: >=3 projects, one
phase with >50 tasks, cross-project depends_on, open + answered questions,
a non-empty decisions list (forces the "new UI" CDN path in plangraph.render),
activity events, and one item whose ``loop`` is an object.

Usage: ``python tools/gen_ui_snapshot.py [--out tests/fixtures/ui_snapshot.json]``
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

GENERATED = "2026-06-15T12:00:00Z"
HARBOR_TASK_COUNT = 55  # >50 tasks in one phase (§ W1 brief)


def _ts(day: int, hour: int = 9) -> str:
    return f"2026-06-{day:02d}T{hour:02d}:00:00Z"


def item(iid: str, project: str, phase: str, kind: str, state: str, title: str,
         *, deps=None, owner=None, estimate_h=2.0, loop=None, created_day=1,
         done_day=None) -> dict:
    """One snapshot item, full examples/snapshot.json key set."""
    done = state == "done"
    return {
        "agent_id": None,
        "answer": None,
        "choices": None,
        "claimed_by": owner if state in ("claimed", "in_review") else None,
        "corrected_by": [],
        "corrects": None,
        "created_at": _ts(created_day),
        "depends_on": list(deps or []),
        "description": None,
        "done_at": _ts(done_day or created_day) if done else None,
        "done_criteria": None,
        "epoch": 1,
        "estimate_h": estimate_h,
        "estimate_src": "default",
        "evidence": [],
        "evidence_kinds": [],
        "fixed_by": [],
        "fixes": None,
        "host": None,
        "id": iid,
        "key": f"{project}:{iid}",
        "kg_refs": [],
        "kind": kind,
        "layer": None,
        "lease_until": None,
        "merged": False,
        "model": None,
        "note": None,
        "operator": False,
        "owner": owner or "tess (fictional)",
        "phase": phase,
        "phase_kind": None,
        "priority": 0,
        "project": project,
        "review_tier": "standard",
        "review_waiver": None,
        "reviewed": False,
        "reviewer_run": None,
        "reviews": [],
        "role": None,
        "run_ids": [],
        "seat_models": None,
        "source": f"plans/{project}.md#{iid}",
        "state": state,
        "title": title,
        "track": phase,
        "updated_at": _ts(done_day or created_day, 17),
        "wait": None,
        "waits_on_operator": False,
        "worker_run": None,
        "writer_model": None,
        "loop": loop,
    }


def build_items() -> list:
    items = []
    # -- lighthouse: small project, depends across to orchard ----------------
    items.append(item("LH-1", "lighthouse", "T1", "lane", "done",
                      "Chart the harbour approaches", owner="ada (fictional)",
                      estimate_h=4.0, created_day=1, done_day=3))
    items.append(item("LH-GOAL", "lighthouse", "goal", "goal", "open",
                      "Lighthouse guide online", deps=["LH-1"], owner="tess (fictional)",
                      estimate_h=0.0))
    items.append(item("LH-2", "lighthouse", "T1", "lane", "in_review",
                      "Polish the lens optics", deps=["LH-1", "orchard:OR-2"],
                      owner="ada (fictional)", created_day=4))
    items.append(item("LH-3", "lighthouse", "T2", "lane", "open",
                      "Trim the wick mechanism", deps=["LH-2"], created_day=6))
    # -- orchard: medium project with a gate ----------------------------------
    items.append(item("OR-1", "orchard", "T1", "lane", "claimed",
                      "Survey the seed catalogue", owner="otto (fictional)",
                      created_day=2))
    items.append(item("OR-2", "orchard", "T1", "lane", "done",
                      "Sketch the terrace beds", owner="otto (fictional)",
                      created_day=2, done_day=5))
    items.append(item("OR-3", "orchard", "T2", "lane", "open",
                      "Set the irrigation lines", deps=["OR-2", "lighthouse:LH-3"], created_day=7))
    items.append(item("OR-4", "orchard", "T3", "gate", "open",
                      "First harvest review", deps=["OR-3"], created_day=8))
    items.append(item("OR-GOAL", "orchard", "goal", "goal", "open",
                      "Orchard terrace planted", deps=["OR-4"], owner="tess (fictional)",
                      estimate_h=0.0))
    # -- harbor: the >50-task phase (P1) + a looping item ---------------------
    prev = None
    for n in range(1, HARBOR_TASK_COUNT + 1):
        state = "done" if n <= 20 else ("claimed" if n == 21 else "open")
        loop = {"iter": 2, "max": 5} if n == 30 else None
        items.append(item(f"HB-{n:02d}", "harbor", "P1", "lane", state,
                          f"Dredge quay segment {n:02d}",
                          deps=[f"HB-{n - 1:02d}"] if prev else ["orchard:OR-2"],
                          owner="mira (fictional)" if state != "open" else "tess (fictional)",
                          estimate_h=1.0, created_day=3, loop=loop))
        prev = f"HB-{n:02d}"
    items.append(item("HB-56", "harbor", "P2", "lane", "open",
                      "Relight the breakwater beacons",
                      deps=["HB-55", "lighthouse:LH-3"], created_day=9))
    items.append(item("HB-GOAL", "harbor", "goal", "goal", "open",
                      "Harbor quay open", deps=["HB-56"], owner="tess (fictional)",
                      estimate_h=0.0))
    # keys must be project-qualified like the keeper export
    for it in items:
        if ":" not in it["key"]:
            it["key"] = f"{it['project']}:{it['id']}"
        it["depends_on"] = [d if ":" in d else
                            next(i["key"] for i in items
                                 if i["project"] == it["project"] and i["id"] == d)
                            for d in it["depends_on"]]
    return items


def build_questions() -> list:
    return [
        {"id": "Q-1", "project": "lighthouse", "text": "Q: Which lamp oil spec?",
         "asked_by": "ada (fictional)", "status": "open", "item": "lighthouse:LH-2",
         "answer": None},
        {"id": "Q-2", "project": "harbor", "text": "Q: Dredging window dates?",
         "asked_by": "mira (fictional)", "status": "open", "item": "harbor:HB-01",
         "answer": None},
        {"id": "Q-3", "project": "orchard", "text": "Q: Terrace soil depth?",
         "asked_by": "otto (fictional)", "status": "answered", "item": "orchard:OR-2",
         "answer": "90 cm over the drainage mat.",
         "who": "tess (fictional)", "when": _ts(4, 10), "decision": "D-11"},
        {"id": "Q-4", "project": "harbor", "text": "Q: Quay load class?",
         "asked_by": "mira (fictional)", "status": "answered", "item": "harbor:HB-10",
         "answer": "Class B per the fictional harbour code.",
         "who": "tess (fictional)", "when": _ts(6, 11), "decision": "D-12"},
    ]


def build_decisions() -> list:
    return [
        {"id": "D-11", "project": "orchard", "title": "D-11: Terrace soil depth fixed at 90 cm",
         "text": "Soil depth is 90 cm over the drainage mat for every terrace bed.",
         "date": "2026-06-04", "items": ["orchard:OR-2", "orchard:OR-3"]},
        {"id": "D-12", "project": "harbor", "title": "D-12: Quay load class B",
         "text": "The quay is built to load class B of the fictional harbour code.",
         "date": "2026-06-06", "items": ["harbor:HB-05", "harbor:HB-10"]},
    ]


def build_activity(items: list) -> list:
    ev = []
    for i, it in enumerate(items[:12]):
        ev.append({"actor": "router", "key": it["key"], "state": "open",
                   "ts": _ts(1 + (i % 9)), "type": "item_created"})
    ev.append({"actor": "ada (fictional)", "key": "lighthouse:LH-2",
               "ts": _ts(5), "type": "submitted"})
    ev.append({"actor": "otto (fictional)", "key": "orchard:OR-1",
               "ts": _ts(6), "type": "claimed"})
    ev.append({"actor": "tess (fictional)", "key": "orchard:OR-2", "state": "done",
               "ts": _ts(5), "type": "closed"})
    return ev


def build_progress(items: list) -> dict:
    projects = []
    for pname in ("harbor", "lighthouse", "orchard"):
        mine = [i for i in items if i["project"] == pname]
        phases, byphase = [], {}
        for it in mine:
            byphase.setdefault(it["phase"], []).append(it)
        for ph in sorted(byphase):
            lst = byphase[ph]
            done = sum(1 for x in lst if x["state"] == "done")
            total_h = sum(x["estimate_h"] for x in lst)
            done_h = sum(x["estimate_h"] for x in lst if x["state"] == "done")
            phases.append({"done": done, "hours_done": done_h,
                           "hours_left": round(total_h - done_h, 2), "hours_total": total_h,
                           "pct_count": round(100.0 * done / len(lst), 1),
                           "pct_hours": round(100.0 * done_h / total_h, 1) if total_h else 0.0,
                           "phase": ph, "total": len(lst)})
        states = {s: sum(1 for x in mine if x["state"] == s)
                  for s in ("cancelled", "claimed", "done", "in_review", "open")}
        total_h = sum(x["estimate_h"] for x in mine)
        done_h = sum(x["estimate_h"] for x in mine if x["state"] == "done")
        done = states["done"]
        projects.append({
            "done": done, "hours_done": done_h,
            "hours_left": round(total_h - done_h, 2), "hours_total": total_h,
            "last_update": GENERATED, "operator_open": 0,
            "pct_count": round(100.0 * done / len(mine), 1),
            "pct_hours": round(100.0 * done_h / total_h, 1) if total_h else 0.0,
            "phases": phases, "project": pname,
            "running": [{"key": x["key"], "model": None, "owner": x["owner"],
                         "state": x["state"], "title": x["title"]}
                        for x in mine if x["state"] in ("claimed", "in_review")],
            "states": states, "total": len(mine)})
    tot = [i for i in items]
    done = sum(1 for x in tot if x["state"] == "done")
    total_h = sum(x["estimate_h"] for x in tot)
    done_h = sum(x["estimate_h"] for x in tot if x["state"] == "done")
    return {
        "projects": projects,
        "total": {"done": done, "hours_done": done_h,
                  "hours_left": round(total_h - done_h, 2), "hours_total": total_h,
                  "pct_count": round(100.0 * done / len(tot), 1),
                  "pct_hours": round(100.0 * done_h / total_h, 1), "total": len(tot)},
    }


def build_snapshot() -> dict:
    items = build_items()
    return {
        "activity": build_activity(items),
        "aliases": {},
        "decision_text": {"D-11": "Terrace soil depth fixed at 90 cm.",
                          "D-12": "Quay load class B per the fictional harbour code."},
        "decisions": build_decisions(),
        "generated": GENERATED,
        "head": None,
        "head_seq": 24,
        "items": items,
        "progress": build_progress(items),
        "questions": build_questions(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1]
                                             / "tests" / "fixtures" / "ui_snapshot.json"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build_snapshot(), indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
