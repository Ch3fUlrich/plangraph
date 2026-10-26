"""§0.2e review round counter and cap, as a pure fold over an item's events.

A round is one review that came back with findings: a keeper
``changes_requested`` event, or an ``item_logged`` ``fix`` entry (the L1 logs
the seats' findings).  A fix entry that answers a ``changes_requested`` with no
fix entry in between is the same round (design invariant I3).  A ``replan``
entry resets the since-replan count, never the total (I2).
"""
from __future__ import annotations

__all__ = ["ROUND_CAP", "count_rounds", "cap_reached"]

ROUND_CAP = 3


def count_rounds(events):
    """``{"rounds", "since_replan", "replans", "design", "fix_log"}`` from *events*.

    *events* are ``{"type", "payload", "ts"?}`` mappings in log order;
    ``payload`` is a dict.  ``design`` is the newest ``design`` entry (or None),
    ``fix_log`` the logged entries in order.
    """
    rounds = since = replans = 0
    pending = False
    design = None
    log = []
    for event in events:
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "changes_requested":
            rounds += 1
            since += 1
            pending = True
        elif kind == "item_logged":
            entry = dict(payload, ts=event.get("ts"))
            log.append(entry)
            sort = payload.get("kind")
            if sort == "fix":
                if pending:
                    pending = False
                else:
                    rounds += 1
                    since += 1
            elif sort == "replan":
                replans += 1
                since = 0
                pending = False
                design = entry
            elif sort == "design":
                design = entry
    return {"rounds": rounds, "since_replan": since, "replans": replans,
            "design": design, "fix_log": log}


def cap_reached(summary):
    """True when the item needs a replan before another writer/fixer run."""
    return int(summary.get("since_replan") or 0) >= ROUND_CAP
