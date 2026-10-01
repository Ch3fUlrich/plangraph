"""D-217 waiver verb for :class:`plangraph.keeper.store.Store`.

The two-reviewer gate (D-219, ``store_review``) normally requires the second
passing verdict to come from a different family.  When that seat is unavailable
the router may record a **verified ``decision``** that waives the second-family
requirement for one item: after ``waive_second_family(principal, key, D-NNN)``
the two passing verdicts may share a family.  Distinct principals, non-null
families and the claimant/delegate exclusion all still apply — the waiver only
relaxes *which families* count.

Like ``add_evidence_router`` the slow decision verification runs before the
write lock, and the row is re-read and guarded inside ``BEGIN IMMEDIATE``.
"""
from __future__ import annotations

from .errors import Refused


class WaiverMixin:
    """The router-only ``waive_second_family`` verb."""

    def waive_second_family(self, principal, key, decision_id):
        return self._run(principal, "waive_second_family", key,
                         lambda now: self._waive(principal, key, decision_id,
                                                 now))

    def _waive(self, principal, key, decision_id, now):
        self._require_role(principal, "router")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}, not active")
        if not isinstance(decision_id, str) or not decision_id:
            raise Refused("validation", "decision_id is required")
        # The slow decision lookup runs before the write lock, exactly as
        # ``add_evidence_router`` verifies its evidence.
        result = self._verify_evidence(item, "decision", {"id": decision_id}, now)
        if not result.ok:
            raise Refused("evidence_unverified",
                          result.detail or f"decision {decision_id} unverified")
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] not in ("claimed", "in_review"):
                raise Refused("conflict", f"{key} is {fresh['state']}")
            cursor = self.conn.execute(
                "UPDATE items SET review_waiver=?, updated_at=?"
                " WHERE key=? AND epoch=?"
                " AND state IN ('claimed','in_review')",
                (decision_id, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, fresh["state"], item["epoch"])
            self._append(principal, key, "waiver_set",
                         {"decision_id": decision_id}, now)
            return {"key": key, "state": fresh["state"],
                    "review_waiver": decision_id}
