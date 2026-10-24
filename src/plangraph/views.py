"""Views API for the plan graph — pure functions over a list of item dicts.

Implements the "Views API" section of ``docs/SPEC.md``.  No I/O, no globals,
no randomness: every output is sorted so a snapshot always renders the same
way.  The graph maths lives in :mod:`plangraph.views_graph`.

Item shape (all keys fully qualified) is documented in ``docs/SPEC.md``.
"""
from __future__ import annotations

import re

from . import views_graph as _g

__all__ = [
    "index",
    "validate",
    "warnings",
    "ready",
    "blocked",
    "waves",
    "critical_path",
    "slack",
    "percent_done",
    "unblocks",
    "brief",
    "TERMINAL",
    "STATES",
]

STATES = ("open", "ready", "claimed", "in_review", "done", "cancelled", "running",
          "needs_input", "waiting", "failed", "parked")
TERMINAL = frozenset(("done", "cancelled"))
_ACTIVE = ("open", "claimed")


# ---------------------------------------------------------------- helpers

def _est(it):
    value = it.get("estimate_h", 0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _prio(it):
    try:
        return int(it.get("priority", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _state(it):
    return it.get("state")


def _deps(it):
    return list(it.get("depends_on") or [])


def _problem(code, key, msg):
    return {"code": code, "key": key, "msg": msg}


def _clean(value):
    return 0.0 if abs(value) < 1e-9 else round(value, 9)


# ---------------------------------------------------------------- API

def index(items):
    """Map ``key -> item``.  A duplicate key keeps the last occurrence."""
    return {it["key"]: it for it in items}


def validate(items):
    """Whole-graph validation; returns a sorted list of Problem dicts.

    Problem codes: ``dup_key``, ``unknown_dep``, ``cycle``,
    ``depends_on_cancelled``, ``no_goal``, ``unreachable_goal``, ``bad_state``,
    ``corrects_not_done``, ``self_dep``, ``loop_no_exit``, ``loop_many_exits``.
    A ``decision`` item that depends on a ``loop`` is that loop's exit decision:
    every non-cancelled ``loop`` needs exactly one such decision, and only
    decisions whose state is not ``cancelled`` are counted.
    """
    idx = index(items)
    problems = []

    for it in items:
        key = it["key"]
        if _state(it) not in STATES:
            problems.append(_problem("bad_state", key,
                                     f"state {it.get('state')!r} is not allowed"))

    counts = {}
    for it in items:
        counts[it["key"]] = counts.get(it["key"], 0) + 1
    for key in counts:
        if counts[key] > 1:
            problems.append(_problem("dup_key", key,
                                     f"key appears {counts[key]} times"))

    for it in items:
        key = it["key"]
        for dep in _deps(it):
            if dep == key:
                problems.append(_problem("self_dep", key, "depends on itself"))
            target = idx.get(dep)
            if target is None:
                problems.append(_problem("unknown_dep", key,
                                         f"depends on unknown key {dep}"))
            elif _state(target) == "cancelled":
                problems.append(_problem("depends_on_cancelled", key,
                                         f"depends on cancelled item {dep}"))

    for it in items:
        key = it["key"]
        corrects = it.get("corrects")
        if not corrects:
            continue
        target = idx.get(corrects)
        if target is None:
            problems.append(_problem("corrects_not_done", key,
                                     f"corrects unknown key {corrects}"))
        elif _state(target) != "done":
            problems.append(_problem(
                "corrects_not_done", key,
                f"corrects {corrects} which is {_state(target)}"))

    # MODEL-LOOP: a decision depending on a loop is its exit decision; every
    # live loop needs exactly one (cancelled decisions are not counted).
    live_decisions = [d for d in items
                      if d.get("kind") == "decision" and _state(d) != "cancelled"]
    for it in items:
        if it.get("kind") != "loop" or _state(it) == "cancelled":
            continue
        key = it["key"]
        exits = [d for d in live_decisions if key in _deps(d)]
        if not exits:
            problems.append(_problem("loop_no_exit", key,
                                     "no exit decision depends on this loop"))
        elif len(exits) > 1:
            problems.append(_problem(
                "loop_many_exits", key,
                f"loop has {len(exits)} exit decisions, only one allowed"))

    non_terminal = [it for it in items if _state(it) not in TERMINAL]
    goals = [it for it in items if it.get("kind") == "goal"]
    if non_terminal and not goals:
        problems.append(_problem("no_goal", "",
                                 "no goal item exists while work remains"))
    if goals and non_terminal:
        preds = {k: [d for d in _deps(idx[k]) if d in idx] for k in idx}
        reaches = _g.ancestors({it["key"] for it in goals}, preds)
        for it in non_terminal:
            if it["key"] not in reaches:
                problems.append(_problem("unreachable_goal", it["key"],
                                         "no path to a goal"))

    nodes = sorted(idx)
    succ = {k: [d for d in _deps(idx[k]) if d in idx] for k in nodes}
    for cycle in _g.find_cycles(nodes, succ):
        rendered = " -> ".join(cycle + [cycle[0]])
        problems.append(_problem("cycle", cycle[0], f"cycle: {rendered}"))

    seen = set()
    unique = []
    for problem in sorted(problems, key=lambda p: (p["code"], p["key"], p["msg"])):
        token = (problem["code"], problem["key"], problem["msg"])
        if token not in seen:
            seen.add(token)
            unique.append(problem)
    return unique


_ID_ONLY = re.compile(r"^[A-Z0-9-]+$")


def warnings(items):
    """Advisory findings (never errors) for active items, sorted.

    ``title_is_id``: the title is only an id-looking token (``^[A-Z0-9-]+$``);
    ``no_description``: the item has no description.  Done and cancelled items
    are locked and never warned about.
    """
    out = []
    for it in items:
        if _state(it) in TERMINAL:
            continue
        key = it["key"]
        if _ID_ONLY.match(it.get("title") or ""):
            out.append(_problem("title_is_id", key,
                                "title is only an id; give it a name"))
        if not (it.get("description") or "").strip():
            out.append(_problem("no_description", key, "no description"))
    return sorted(out, key=lambda p: (p["code"], p["key"]))


def ready(items, project=None, ub=None, owner=None, agent=False):
    """Open items whose every dependency is ``done``, best first.

    *ub* is a precomputed :func:`unblocks` map over the FULL graph, for a caller
    that holds only a subset of the items.  *owner* keeps only items owned by
    that principal; *agent* drops what waits on the operator (``operator`` flag
    or an operator kind), so a heartbeat never picks a human step (D-953).

    Sorted by ``(-priority, -unblocks, estimate_h, key)``.  Gates and goals are
    included when open and unblocked: closing them is a valid action.
    """
    idx = index(items)
    ub = unblocks(items) if ub is None else ub
    out = [it for it in items
           if _state(it) == "open"
           and (project is None or it.get("project") == project)
           and (owner is None or it.get("owner") == owner)
           and not (agent and (it.get("operator") or it.get("kind") in OPERATOR_KINDS))
           and all(idx.get(d, {}).get("state") == "done" for d in _deps(it))]
    out.sort(key=lambda it: (-_prio(it), -ub.get(it["key"], 0), _est(it), it["key"]))
    return out


def blocked(items, project=None):
    """Open/claimed items with unmet dependencies -> sorted unmet keys.

    Items with no unmet dependency are omitted (they are not blocked).
    """
    idx = index(items)
    out = {}
    for it in sorted(items, key=lambda x: x["key"]):
        if _state(it) not in _ACTIVE:
            continue
        if project is not None and it.get("project") != project:
            continue
        unmet = sorted(d for d in _deps(it)
                       if idx.get(d, {}).get("state") != "done")
        if unmet:
            out[it["key"]] = unmet
    return out


def waves(items):
    """Layers over non-terminal items; wave 0 is ready-now.

    ``done`` dependencies are satisfied and add no edge.  A non-terminal item
    whose unmet dependency is itself not schedulable (cancelled or unknown) is
    omitted, together with its dependents; a validated graph never hits this.
    """
    idx = index(items)
    nodes = {k for k, it in idx.items() if _state(it) not in TERMINAL}
    changed = True
    while changed:
        changed = False
        for key in list(nodes):
            if any(d not in nodes and idx.get(d, {}).get("state") != "done"
                   for d in _deps(idx[key])):
                nodes.discard(key)
                changed = True
    preds = {k: {d for d in _deps(idx[k]) if d in nodes} for k in nodes}
    return _g.layers(sorted(nodes), preds)


def critical_path(items, project=None):
    """Longest remaining path by ``estimate_h`` over non-terminal items.

    Returns ``{"keys": [prerequisite, ..., endpoint], "hours": float}``.
    ``project`` restricts the nodes; ties pick the smallest key sequence.
    """
    idx = index(items)
    nodes = sorted(k for k, it in idx.items()
                   if _state(it) not in TERMINAL
                   and (project is None or it.get("project") == project))
    if not nodes:
        return {"keys": [], "hours": 0.0}
    nodeset = set(nodes)
    preds = {k: {d for d in _deps(idx[k]) if d in nodeset} for k in nodes}
    hours, path = _g.longest_paths(nodes, preds, lambda k: _est(idx[k]))
    best = max(hours.values())
    candidates = [path[k] for k in nodes if hours[k] == best]
    best_path = min(candidates, key=lambda p: (-len(p), p))
    return {"keys": best_path, "hours": float(best)}


def slack(items):
    """Hours of slack per non-terminal item; 0 on the critical path.

    Slack is ``critical_hours - longest_path_through(item)``.
    """
    idx = index(items)
    nodes = sorted(k for k, it in idx.items() if _state(it) not in TERMINAL)
    if not nodes:
        return {}
    nodeset = set(nodes)
    preds = {k: {d for d in _deps(idx[k]) if d in nodeset} for k in nodes}
    succ = {k: set() for k in nodes}
    for k in nodes:
        for d in preds[k]:
            succ[d].add(k)
    weight = lambda k: _est(idx[k])
    forward, _ = _g.longest_paths(nodes, preds, weight)
    backward, _ = _g.longest_paths(nodes, succ, weight)
    critical = max(forward.values())
    out = {k: _clean(critical - (forward[k] + backward[k] - weight(k)))
           for k in nodes}
    return dict(sorted(out.items()))


def percent_done(items, by="estimate", project=None):
    """Completion as a percentage in ``[0, 100]`` (see SPEC ambiguity note).

    ``by="count"`` counts items; ``by="estimate"`` sums ``estimate_h``.
    Cancelled items are excluded from both numerator and denominator.  An empty
    (or fully cancelled) selection is ``0.0``.
    """
    if by not in ("estimate", "count"):
        raise ValueError(f"unknown metric {by!r}; use 'estimate' or 'count'")
    selected = [it for it in items
                if _state(it) != "cancelled"
                and (project is None or it.get("project") == project)]
    if not selected:
        return 0.0
    if by == "count":
        numerator = sum(1 for it in selected if _state(it) == "done")
        denominator = len(selected)
    else:
        numerator = sum(_est(it) for it in selected if _state(it) == "done")
        denominator = sum(_est(it) for it in selected)
    if denominator == 0:
        return 0.0
    return 100.0 * numerator / denominator


def unblocks(items):
    """For every key, how many non-terminal items directly wait on it."""
    idx = index(items)
    counts = {k: 0 for k in idx}
    for it in items:
        if _state(it) in TERMINAL:
            continue
        for dep in set(_deps(it)):
            if dep in counts:
                counts[dep] += 1
    return dict(sorted(counts.items()))


def brief(items, project=None, n=10, ub=None, owner=None, agent=False):
    """Agent text: one line per ready item, capped at *n*.

    Format: ``key | title | est Nh | unblocks N | prio P``.
    """
    ub = unblocks(items) if ub is None else ub
    rows = ready(items, project=project, ub=ub, owner=owner, agent=agent)
    if n is not None:
        rows = rows[:max(0, int(n))]
    return brief_rows(rows, ub)


def brief_rows(rows, ub):
    """The :func:`brief` lines for *rows* exactly as given (no filtering, no cap)."""
    return "\n".join(f"{it['key']} | {it.get('title', '')} | "
                     f"est {_est(it):g}h | unblocks {ub.get(it['key'], 0)} | "
                     f"prio {_prio(it)}" for it in rows)


#: MK-READS: kinds that always wait on the operator, whatever their ``operator`` flag.
OPERATOR_KINDS = ("question", "decision", "operator_task")


def needs_you(items, project=None, ub=None):
    """What waits on the operator, most-blocking first.

    Non-terminal items with ``operator`` set or an operator kind, ranked by
    ``(-unblocks, -priority, key)``; each row is a small plain dict.
    """
    ub = unblocks(items) if ub is None else ub
    picked = [it for it in items
              if _state(it) not in TERMINAL
              and (project is None or it.get("project") == project)
              and (it.get("operator") or it.get("kind") in OPERATOR_KINDS)]
    picked.sort(key=lambda it: (-ub.get(it["key"], 0), -_prio(it), it["key"]))
    return [{"key": it["key"], "project": it.get("project"),
             "title": it.get("title", ""), "kind": it.get("kind"),
             "state": _state(it), "wait": it.get("wait"),
             "priority": _prio(it), "unblocks": ub.get(it["key"], 0),
             "choices": _choices(it.get("choices"))}
            for it in picked]


def _choices(value):
    """QA-IMPORT: the structured options of a question (dict from a snapshot, JSON text from the DB)."""
    if isinstance(value, dict) or value is None:
        return value
    from .keeper.item_fields import parse_choices
    return parse_choices(value)


def answers(items, project=None):
    """Answered question/decision items, newest first.

    An item is answered when it is a question/decision (or a QUESTIONS row that
    is not an operator task, SYNC-MD-IMPORT), ``done`` and carries ``answer`` (``{"text",
    "decision_id"}``) and ``done_at`` (the store joins them from the ``answered``
    event).  A row whose item is named by another live item's ``corrects`` gets
    ``superseded_by`` (sorted keys of those items).
    """
    corrected_by = {}
    for it in items:
        if it.get("corrects") and _state(it) != "cancelled":
            corrected_by.setdefault(it["corrects"], []).append(it["key"])
    rows = []
    for it in items:
        answer = it.get("answer")
        asked = (it.get("kind") in ("question", "decision")
                 or (it.get("track") == "QUESTIONS" and it.get("wait") != "task"
                     and it.get("kind") not in ("gate", "goal", "operator_task")))
        if (not asked or _state(it) != "done"
                or not answer
                or (project is not None and it.get("project") != project)):
            continue
        row = {"key": it["key"], "project": it.get("project"),
               "title": it.get("title", ""), "kind": it.get("kind"),
               "answer": answer.get("text"),
               "decision_id": answer.get("decision_id"),
               "done_at": it.get("done_at")}
        if corrected_by.get(it["key"]):
            row["superseded_by"] = sorted(corrected_by[it["key"]])
        rows.append(row)
    rows.sort(key=lambda r: r["key"])
    rows.sort(key=lambda r: r["done_at"] or "", reverse=True)
    return rows
