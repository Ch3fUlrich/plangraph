"""PROGRESS-PANEL (router D-547): a standing progress view per project and phase.

Pure function over snapshot-shaped items: no I/O, no clock unless *now* is given.
Gates and goals are structure, not work, so only the other kinds are counted.
``model`` is the writer model the spawner recorded (PG-MODELS); ``None`` until
the L1s write runs through the keeper.
"""
from __future__ import annotations

__all__ = ["progress"]

_STRUCTURE = ("gate", "goal")
_STATES = ("open", "claimed", "in_review", "done", "cancelled")


def _hours(it):
    value = it.get("estimate_h")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def _bucket():
    return {"total": 0, "done": 0, "hours_total": 0.0, "hours_done": 0.0}


def _add(bucket, it):
    hours, done = _hours(it), it.get("state") == "done"
    bucket["total"] += 1
    bucket["hours_total"] += hours
    if done:
        bucket["done"] += 1
        bucket["hours_done"] += hours


def _finish(bucket):
    out = dict(bucket)
    out["hours_total"] = round(out["hours_total"], 1)
    out["hours_done"] = round(out["hours_done"], 1)
    out["hours_left"] = round(out["hours_total"] - out["hours_done"], 1)
    out["pct_count"] = _pct(out["done"], out["total"])
    out["pct_hours"] = _pct(out["hours_done"], out["hours_total"])
    return out


def _latest(it):
    stamps = [it.get(name) for name in ("updated_at", "done_at", "created_at")]
    stamps = [s for s in stamps if isinstance(s, str) and s]
    return max(stamps) if stamps else None


def progress(items, project=None):
    """``{"projects": [...], "total": {...}}`` sorted by project name.

    Per project: count/hour totals and percents, ``states`` counts,
    ``operator_open`` (open items waiting on the operator), ``running`` (claimed
    or in review: key, title, owner, state, model), ``last_update`` and
    ``phases`` (by ``phase`` or else ``track``) with the same totals.
    Cancelled items are left out of the totals but counted in ``states``.
    """
    projects = {}
    total = _bucket()
    for it in items or []:
        if not isinstance(it, dict) or it.get("kind") in _STRUCTURE:
            continue
        name = it.get("project") or "(none)"
        if project and name != project:
            continue
        row = projects.setdefault(name, {"bucket": _bucket(), "phases": {},
                                         "states": {s: 0 for s in _STATES},
                                         "operator_open": 0, "running": [],
                                         "last_update": None})
        state = it.get("state") or "open"
        row["states"][state] = row["states"].get(state, 0) + 1
        stamp = _latest(it)
        if stamp and (row["last_update"] is None or stamp > row["last_update"]):
            row["last_update"] = stamp
        if state == "cancelled":
            continue
        _add(row["bucket"], it)
        _add(total, it)
        phase = it.get("phase") or it.get("track") or "-"
        _add(row["phases"].setdefault(phase, _bucket()), it)
        if it.get("operator") and state not in ("done", "cancelled"):
            row["operator_open"] += 1
        if state in ("claimed", "in_review"):
            row["running"].append({"key": it.get("key"), "title": it.get("title"),
                                   "owner": it.get("claimed_by") or it.get("owner"),
                                   "state": state, "model": it.get("writer_model")})
    out = []
    for name in sorted(projects):
        row = projects[name]
        entry = _finish(row["bucket"])
        entry.update({"project": name, "states": row["states"],
                      "operator_open": row["operator_open"],
                      "running": sorted(row["running"], key=lambda r: r["key"] or ""),
                      "last_update": row["last_update"],
                      "phases": [dict(_finish(b), phase=p)
                                 for p, b in sorted(row["phases"].items())]})
        out.append(entry)
    return {"projects": out, "total": _finish(total)}
