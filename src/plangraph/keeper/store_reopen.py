"""PG-REOPEN: the router takes an ``in_review`` item back to its claimant at a fresh epoch.

``reopen(principal, key, reason, decision_id)`` moves ``in_review -> claimed``
and opens a **new work epoch** (+1, ``round`` reset, ``reviewer`` and
``review_waiver`` cleared, fresh default lease) with one hash-chained
``item_reopened`` event.  The verdicts, the rounds and the evidence that were
accumulated under the old epoch belong to work that is being redone, so
everything the new epoch relies on must be attached again: a submit or a close
still pointing at the superseded epoch fails closed afterwards.  It never
closes an item and never passes or fails a review.

Guards: router principal only; a non-empty reason of at most 500 characters; a
``D-NNN`` decision id that is recorded in the decisions file (no ``Closes:``
naming is required -- unlike ``withdraw_review``, the reason states why the
work is being redone); and the item must be ``in_review``.
"""
from __future__ import annotations

import re

from .errors import Refused

__all__ = ["ReopenMixin"]

_REASON_MAX = 500


class ReopenMixin:
    """Router-only ``reopen``."""

    def reopen(self, principal, key, reason, decision_id):
        return self._run(principal, "reopen", key,
                         lambda now: self._reopen(principal, key, reason,
                                                  decision_id, now))

    def _reopen(self, principal, key, reason, decision_id, now):
        self._require_role(principal, "router")
        if not isinstance(reason, str) or not reason.strip():
            raise Refused("validation", "reason must be a non-empty string")
        if len(reason) > _REASON_MAX:
            raise Refused("validation",
                          f"reason must be at most {_REASON_MAX} characters")
        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "decision_id must look like D-NNN")
        item = self._get_or_refuse(key)
        # State first: a finished or idle item is a bad_state, whatever its
        # lock status, so the caller learns what it must do instead.
        if item["state"] != "in_review":
            raise Refused("bad_state", f"{key} is {item['state']}, not in_review")
        self._assert_unlocked(item)
        decisions = self._decisions_adapter(item, now)
        try:
            recorded = decisions.has(decision_id)
        except Exception:  # noqa: BLE001 - an unreadable decisions file fails closed
            recorded = False
        if not recorded:
            raise Refused("evidence_unverified",
                          f"decision {decision_id} is not in the decisions file")
        lease = self._iso(now + self.default_lease)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != "in_review" or fresh["epoch"] != item["epoch"]:
                raise Refused("conflict", f"{key} changed under the transaction")
            new_epoch = fresh["epoch"] + 1
            claimed_by = fresh["claimed_by"]
            self._append(principal, key, "item_reopened",
                         {"reason": reason, "decision_id": decision_id,
                          "claimed_by": claimed_by, "epoch": new_epoch,
                          "lease_until": lease}, now)
            # Mirror the ``claimed`` event's column set on the epoch move
            # (round/waiver/reviewer cleared, holder kept), guarded on the
            # state and epoch read above so a lost race refuses.
            cursor = self.conn.execute(
                "UPDATE items SET state='claimed', epoch=epoch+1, round=0,"
                " reviewer=NULL, review_waiver=NULL, lease_until=?, updated_at=?"
                " WHERE key=? AND state='in_review' AND epoch=?",
                (lease, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, "in_review", item["epoch"])
            self._exec_clear(self.conn, key, principal, now)
        return {"key": key, "state": "claimed", "epoch": new_epoch,
                "claimed_by": claimed_by, "lease_until": lease}
