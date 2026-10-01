"""Pure read-only fallbacks computed from a local ``plan.snapshot.json``.

Used only when the keeper is unreachable: the snapshot already carries every
field these helpers need, and :mod:`plangraph.views` supplies the graph maths,
so the CLI can still answer read verbs offline.
"""
from __future__ import annotations

from .. import views

__all__ = ["evidence_ref", "local_show", "local_alerts", "local_digest"]


def evidence_ref(args) -> dict:
    """Build the evidence ``ref`` from the CLI flags for one kind."""
    kind = args.kind
    if kind == "commit+ci":
        return {"commit": args.commit}
    if kind == "run-record":
        return {"run_id": args.run}
    if kind == "semaphore-task":
        return {"task_id": args.task}
    if kind == "decision":
        return {"id": args.decision}
    ref = {}
    for key, value in (("commit", args.commit), ("run_id", args.run),
                       ("task_id", args.task), ("id", args.decision)):
        if value:
            ref[key] = value
    return ref


def local_show(items, key) -> dict:
    """The same shape as ``Store.show``, from snapshot items."""
    index = {it["key"]: it for it in items}
    item = index.get(key)
    if item is None:
        raise KeyError(key)
    deps = [{"key": dep, "state": index.get(dep, {}).get("state")}
            for dep in item.get("depends_on") or []]
    unblocks = sorted(o["key"] for o in items
                      if key in (o.get("depends_on") or [])
                      and o.get("state") not in ("done", "cancelled"))
    return {"key": key, "title": item.get("title"), "project": item.get("project"),
            "kind": item.get("kind"), "track": item.get("track"),
            "owner": item.get("owner"), "state": item.get("state"),
            "priority": item.get("priority"), "epoch": item.get("epoch"),
            "claimed_by": item.get("claimed_by"), "delegate": item.get("delegate"),
            "lease_until": item.get("lease_until"), "reviewer": item.get("reviewer"),
            "done_criteria": item.get("done_criteria"),
            "corrects": item.get("corrects"), "depends_on": deps,
            "unblocks": unblocks, "evidence": item.get("evidence") or []}


def local_alerts(items, project) -> dict:
    """A reduced alert set: the snapshot has no audit findings or refusals."""
    scope = [it for it in items
             if project is None or it.get("project") == project]
    ready = views.ready(items, project=project)
    in_flight = any(it.get("state") in ("claimed", "in_review") for it in scope)
    goal_open = any(it.get("kind") == "goal"
                    and it.get("state") not in ("done", "cancelled")
                    for it in scope)
    return {
        "lease_expiring": sorted(
            ({"key": it["key"], "claimed_by": it.get("claimed_by"),
              "lease_until": it.get("lease_until")}
             for it in scope if it.get("state") == "claimed"
             and it.get("lease_until")), key=lambda x: x["key"]),
        "review_overdue": sorted(it["key"] for it in scope
                                 if it.get("state") == "in_review"),
        "findings": [], "refusals": [],
        "stalled": bool(scope) and not ready and not in_flight and goal_open,
    }


def local_digest(items) -> dict:
    counts = {}
    for it in items:
        bucket = counts.setdefault(it.get("project"),
                                   {"open": 0, "claimed": 0, "in_review": 0,
                                    "done": 0, "cancelled": 0, "stalled": False})
        state = it.get("state")
        if state in bucket:
            bucket[state] += 1
    for name in counts:
        counts[name]["stalled"] = local_alerts(items, name)["stalled"]
    done = [it for it in items if it.get("state") == "done"]
    return {"projects": counts,
            "done": [{"key": it["key"], "title": it.get("title"),
                      "evidence_kinds": [e.get("kind") for e in
                                         (it.get("evidence") or [])]}
                     for it in done[-10:]]}
