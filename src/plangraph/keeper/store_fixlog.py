"""PG-EFFICIENCY-GATES ``log_item``: the item's fix log, design and replan records.

D-2739 §0.1 ONE TASK LOOPS: a finding is fixed inside its item.  ``log_item``
appends one ``item_logged`` event (shown under the description by ``show`` as
the fix log); a ``fix`` entry on an ``in_review`` item also hands it back to its
claimant (a ``changes_requested`` event marked ``via: log_item``, same epoch,
fresh lease), so the next round runs under the same key.  ``design`` and
``replan`` entries carry the plan the spawner's plan gate and round cap read
(``gates.rounds``).  Router or the project's L1 only; done/cancelled items are
locked.
"""
from __future__ import annotations

from ..gates.models import STRONG, model_tier
from ..gates.rounds import count_rounds
from .credential_guard import guard
from .errors import Refused

__all__ = ["FixLogMixin", "normalise_entry", "LOG_KINDS"]

LOG_KINDS = ("fix", "design", "replan")
_STR_CAPS = {"note": 500, "ref": 300, "author_model": 128, "reviewed_by": 128,
             "invariants": 4000}
_LIST_CAPS = {"findings": (50, 64), "seat_runs": (8, 128)}
_ALLOWED = frozenset({"kind", "slices"} | set(_STR_CAPS) | set(_LIST_CAPS))
_MAX_SLICES = 20


def _text(entry, name, key, required=False):
    value = entry.get(name)
    if value is None or value == "":
        if required:
            raise Refused("validation", f"{entry.get('kind')} entry needs {name}")
        return None
    if not isinstance(value, str):
        raise Refused("validation", f"{name} must be a string")
    if len(value) > _STR_CAPS[name]:
        raise Refused("validation", f"{name} exceeds {_STR_CAPS[name]} characters")
    guard(name, value, key)
    return value


def _strings(entry, name, key, required=False):
    value = entry.get(name)
    most, cap = _LIST_CAPS[name]
    if value is None:
        if required:
            raise Refused("validation", f"{entry.get('kind')} entry needs {name}")
        return None
    if (not isinstance(value, list) or not 0 < len(value) <= most
            or not all(isinstance(v, str) and 0 < len(v) <= cap for v in value)):
        raise Refused("validation", f"{name} must be 1..{most} strings of <= {cap} characters")
    for v in value:
        guard(name, v, key)
    return list(value)


def _slices(value):
    if value is None:
        return None
    if not isinstance(value, list) or not 0 < len(value) <= _MAX_SLICES:
        raise Refused("validation", f"slices must be a list of 1..{_MAX_SLICES}")
    out = []
    for raw in value:
        if (not isinstance(raw, dict) or set(raw) - {"n", "scope", "max_lines"}
                or not isinstance(raw.get("n"), int) or isinstance(raw.get("n"), bool)
                or not isinstance(raw.get("max_lines"), int) or isinstance(raw.get("max_lines"), bool)
                or raw["max_lines"] <= 0
                or not isinstance(raw.get("scope", ""), str) or len(raw.get("scope", "")) > 200):
            raise Refused("validation", "slice needs int n, int max_lines > 0, scope <= 200")
        out.append({"n": raw["n"], "scope": raw.get("scope", ""), "max_lines": raw["max_lines"]})
    return out


def normalise_entry(entry, key=None):
    """Validate one log entry and return the stored payload (refuses on violation)."""
    if not isinstance(entry, dict):
        raise Refused("validation", "entry must be an object")
    unknown = sorted(set(entry) - _ALLOWED)
    if unknown:
        raise Refused("validation", f"unknown entry fields {unknown}")
    kind = entry.get("kind")
    if kind not in LOG_KINDS:
        raise Refused("validation", f"kind must be one of {list(LOG_KINDS)}")
    out = {"kind": kind}
    fix = kind == "fix"
    plan = not fix
    fields = {
        "note": _text(entry, "note", key, required=fix),
        "findings": _strings(entry, "findings", key, required=fix),
        "seat_runs": _strings(entry, "seat_runs", key),
        "ref": _text(entry, "ref", key, required=plan),
        "author_model": _text(entry, "author_model", key, required=plan),
        "reviewed_by": _text(entry, "reviewed_by", key),
        "invariants": _text(entry, "invariants", key),
        "slices": _slices(entry.get("slices")),
    }
    if plan and model_tier(fields["author_model"]) < STRONG:
        raise Refused("validation",
                      f"{kind} needs a strong author model (D-2739 §0.2a/e), "
                      f"not {fields['author_model']!r}")
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


class FixLogMixin:
    """``log_item`` plus the read-side ``gates`` summary."""

    def log_item(self, principal, key, entry):
        return self._run(principal, "log_item", key,
                         lambda now: self._log_item(principal, key, entry, now))

    def _log_events(self, key):
        import json
        out = []
        for row in self.conn.execute(
                "SELECT type, ts, payload FROM events WHERE key=?"
                " AND type IN ('changes_requested', 'item_logged') ORDER BY seq", (key,)):
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append({"type": row["type"], "ts": row["ts"], "payload": payload})
        return out

    def gates_summary(self, key):
        """``count_rounds`` over the item's events (design invariant I2)."""
        return count_rounds(self._log_events(key))

    def _log_item(self, principal, key, entry, now):
        self._require_role(principal, "router", "l1")
        item = self._get_or_refuse(key)
        self._may_manage(principal, item["project"])
        self._assert_unlocked(item)
        payload = normalise_entry(entry, key)
        handback = payload["kind"] == "fix" and item["state"] == "in_review"
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != item["state"] or int(fresh["epoch"]) != int(item["epoch"]):
                raise Refused("conflict", f"{key} changed under the transaction")
            if handback:
                lease = self._iso(now + self.default_lease)
                self._append(principal, key, "changes_requested",
                             {"state": "claimed", "reason": payload["note"],
                              "lease_until": lease, "epoch": fresh["epoch"],
                              "round": fresh["round"], "via": "log_item"}, now)
                cursor = self.conn.execute(
                    "UPDATE items SET state='claimed', reviewer=?, lease_until=?,"
                    " updated_at=? WHERE key=? AND state='in_review' AND epoch=?",
                    (principal.name, lease, self._iso(now), key, fresh["epoch"]))
                self._race_guard(cursor, key, "in_review", fresh["epoch"])
                self._exec_clear(self.conn, key, principal, now)
            self._append(principal, key, "item_logged", payload, now)
        summary = self.gates_summary(key)
        state = "claimed" if handback else item["state"]
        return {"key": key, "kind": payload["kind"], "state": state,
                "rounds": summary["rounds"], "since_replan": summary["since_replan"]}
