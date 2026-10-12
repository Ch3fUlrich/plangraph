"""The reaper verb for :class:`plangraph.keeper.store.Store`.

Split out of ``store_verbs`` (which sits at the repository's file-size limit):
it only scans every item once and expires lapsed leases or flags reviews that
have run too long, using the shared plumbing on the ``Store`` base class.
"""
from __future__ import annotations


class ReapMixin:
    """Expire lapsed leases and flag reviews that have run too long."""

    # -------------------------------------------------------------- reap
    def reap(self, principal, now=None):
        return self._run(principal, "reap", None,
                         lambda audit_now: self._reap(
                             principal, now if now is not None else audit_now))

    def _reap(self, principal, now):
        self._require_role(principal, "system")
        expired = []
        overdue = []
        with self._write():
            for item in self._all_items():
                if item["state"] == "claimed" and item["lease_until"]:
                    due = self._parse_iso(item["lease_until"])
                    if due is not None and due.timestamp() < now:
                        self._append(principal, item["key"], "lease_expired", {}, now)
                        self.conn.execute(
                            "UPDATE items SET state='open', claimed_by=NULL,"
                            " delegate=NULL, reviewer=NULL, lease_until=NULL,"
                            " review_waiver=NULL, updated_at=? WHERE key=?",
                            (self._iso(now), item["key"]))
                        expired.append(item["key"])
                elif item["state"] == "in_review":
                    since = self._parse_iso(item["updated_at"])
                    if since is not None and now - since.timestamp() > self.review_overdue_s:
                        if not self._has_finding(item["key"], "review_overdue"):
                            self._finding(item["key"], "review_overdue",
                                          "in review for over 4h", now)
                        overdue.append(item["key"])
        return {"expired": expired, "overdue": overdue}
