"""PG-AUDIT-META (D-1012/D-1013): the read verb ``audit_meta(principal, project, stale_days)``.

Which items are missing the metadata a worker needs to start, and which claims
have gone quiet for N days.  The router sees every project; an L1 sees only its
own, so a refusal never names another project's keys or counts.  Everything is
read from the keeper's own rows — evidence, events and principals — with the
injected ``now``, so nothing here touches git, the network or the wall clock.
"""
from __future__ import annotations

from . import audit_meta

__all__ = ["AuditMetaMixin"]


class AuditMetaMixin:
    """``audit_meta`` for :class:`plangraph.keeper.store.Store`."""

    def audit_meta(self, principal, project=None, stale_days=None):
        return self._run(principal, "audit_meta", None,
                         lambda now: self._audit_meta(principal, project,
                                                      stale_days, now))

    def _audit_meta(self, principal, project, stale_days, now):
        days = audit_meta.parse_stale_days(stale_days)   # refuse before any read
        self._require_role(principal, "router", "l1")
        if project is not None:
            self._may_manage(principal, project)         # same code as any cross-project read
            scope = {project}
        elif principal.role == "router":
            scope = None                                 # every project
        else:
            scope = set(principal.projects or [])
        items = [item for item in self._all_items()
                 if scope is None or item.get("project") in scope]
        return audit_meta.build(items, self._last_event_ts(), self._merged_keys(),
                                self._principal_names(), now, self._iso(now), days)

    def _last_event_ts(self):
        """``key -> ts of that key's newest event``."""
        out = {}
        for row in self.conn.execute(
                "SELECT key, ts FROM events WHERE key IS NOT NULL ORDER BY seq"):
            out[row["key"]] = row["ts"]
        return out

    def _merged_keys(self):
        """Keys carrying verified commit evidence — the snapshot's ``merged`` rule."""
        return {row["key"] for row in self.conn.execute(
            "SELECT key FROM evidence WHERE verified!=0 AND kind IN"
            " ('commit+ci','commit') GROUP BY key")}

    def _principal_names(self):
        return {row["name"] for row in
                self.conn.execute("SELECT name FROM principals")}
