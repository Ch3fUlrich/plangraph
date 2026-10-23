"""PG-WITHDRAW-REVIEW (D-867): the router takes an item back out of review.

``withdraw_review(key, decision_id)`` moves ``in_review -> claimed`` (same epoch,
fresh default lease, reviewer cleared) with one hash-chained ``review_withdrawn``
event, so an item whose work turned out to need no commit (an upstream issue, a
Semaphore run) can then be closed on a decision instead of being falsely
"reviewed".  It never passes or fails a review and never touches evidence.

Guards: router principal only; the verified decision must name the key in its
``Closes:`` list; the item's evidence kinds must include ``decision`` or its
done-criteria must not ask for a commit (a code lane cannot dodge its seats).
"""
from __future__ import annotations

import re

from .errors import Refused

__all__ = ["WithdrawMixin"]

_COMMIT_WORDS = re.compile(r"\b(?:commit\w*|merg\w*|sha\w*|ci\w*|seats?|prs?|pull[\s_-]*requests?)\b",
                           re.IGNORECASE)
#: Evidence kinds that never imply code work; anything else (commit, commit+ci, a future
#: kind) counts as a code lane, so the allowlist fails closed.
_NON_CODE_KINDS = frozenset({"decision", "semaphore-task", "run-record"})


class WithdrawMixin:
    """Router-only ``withdraw_review``."""

    def withdraw_review(self, principal, key, decision_id):
        return self._run(principal, "withdraw_review", key,
                         lambda now: self._withdraw_review(principal, key, decision_id, now))

    def _withdraw_review(self, principal, key, decision_id, now):
        self._require_role(principal, "router")
        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "decision_id must look like D-NNN")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "in_review":
            raise Refused("bad_state", f"{key} is {item['state']}, not in_review")
        kinds = item.get("evidence_kinds") or []
        # Eligible: decision is an accepted evidence kind, or neither a code evidence kind
        # (commit, commit+ci, ...) nor commit/merge/CI/seat words in the done-criteria.
        code_lane = any(k not in _NON_CODE_KINDS for k in kinds) or not kinds \
            or _COMMIT_WORDS.search(item.get("done_criteria") or "")
        if "decision" not in kinds and code_lane:
            raise Refused("forbidden",
                          f"{key} needs commit/review evidence; withdraw only decision-closable items")
        decisions = self._decisions_adapter(item, now)
        try:
            recorded = decisions.has(decision_id)
            closes = {self.resolve_key(t) for t in decisions.closes(decision_id)} if recorded else set()
        except Exception:  # noqa: BLE001 - an unreadable decisions file fails closed
            recorded, closes = False, set()
        if not recorded:
            raise Refused("evidence_unverified", f"decision {decision_id} is not in the decisions file")
        if key not in closes:
            raise Refused("evidence_unverified",
                          f"decision {decision_id} does not name {key} in Closes:")
        lease = self._iso(now + self.default_lease)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != "in_review" or fresh["epoch"] != item["epoch"]:
                raise Refused("conflict", f"{key} changed under the transaction")
            self._append(principal, key, "review_withdrawn",
                         {"decision_id": decision_id, "lease_until": lease,
                          "epoch": fresh["epoch"], "round": fresh["round"]}, now)
            cursor = self.conn.execute(
                "UPDATE items SET state='claimed', reviewer=NULL, lease_until=?, updated_at=?"
                " WHERE key=? AND state='in_review' AND epoch=?",
                (lease, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, "in_review", item["epoch"])
            self._exec_clear(self.conn, key, principal, now)
        return {"key": key, "state": "claimed", "epoch": item["epoch"], "lease_until": lease}
