"""The reaper verb for :class:`plangraph.keeper.store.Store`.

Split out of ``store_verbs`` (which sits at the repository's file-size limit):
it only scans every item once and expires lapsed leases or flags reviews that
have run too long, using the shared plumbing on the ``Store`` base class.
"""
from __future__ import annotations

from .. import views
from .store_input import GATE_DEPENDENCY_WARNING, GATE_KINDS


#: KEEPER-PRIORITY-LEVELS-AND-AGING: an open, unclaimed item left at this
#: priority this long since its last priority change drops one level.
AGING_DAYS = {10: 3, 9: 7}


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
        aged = []
        findings = 0
        with self._write():
            items = self._all_items()
            for item in items:
                if item["state"] == "claimed" and item["lease_until"]:
                    due = self._parse_iso(item["lease_until"])
                    if due is not None and due.timestamp() < now:
                        self._append(principal, item["key"], "lease_expired", {}, now)
                        self.conn.execute(
                            "UPDATE items SET state='open', claimed_by=NULL,"
                            " delegate=NULL, reviewer=NULL, lease_until=NULL,"
                             " review_waiver=NULL,"
                            " updated_at=? WHERE key=?",
                             (self._iso(now), item["key"]))
                        self._exec_clear(self.conn, item["key"], principal, now)
                        expired.append(item["key"])
                elif item["state"] == "in_review":
                    since = self._parse_iso(item["updated_at"])
                    if since is not None and now - since.timestamp() > self.review_overdue_s:
                        if not self._has_finding(item["key"], "review_overdue"):
                            self._finding(item["key"], "review_overdue",
                                          "in review for over 4h", now)
                            findings += 1
                        overdue.append(item["key"])
            gate_alerts = self._check_gate_capacity(items, now)
            aged = self._age_priorities(principal, now)
            self._record_inheritance(items, now)
            self._record_categories(items, now)
            alerts = self._alerts_expire(now)
        if expired or aged or findings or alerts or gate_alerts:
            self._reap_revision = getattr(self, "_reap_revision", 0) + 1
        return {"expired": expired, "overdue": overdue, "priority_aged": aged}

    def _record_inheritance(self, items, now):
        """The day's count of items raised by priority inheritance and the longest chain."""
        stats = views.inheritance_stats(views.effective_priorities(items)[0])
        day, stamp = self._iso(now)[:10], self._iso(now)
        for metric, value in (("priority_inherit_raised", stats["raised"]),
                              ("priority_inherit_longest_chain", stats["longest_chain"])):
            self.conn.execute(
                "INSERT INTO metrics(day,metric,value,source,computed_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(day,metric) DO UPDATE SET value=excluded.value,"
                "computed_at=excluded.computed_at", (day, metric, float(value), "reap", stamp))

    def _check_gate_capacity(self, items, now):
        code = "gate_dependency_capacity"
        active = {item["key"] for item in items if item["state"] != "cancelled"}
        near_cap = {}
        for item in items:
            if item["kind"] in GATE_KINDS and item["state"] not in ("done", "cancelled"):
                count = len(active.intersection(item["depends_on"]))
                if count >= GATE_DEPENDENCY_WARNING:
                    near_cap[item["key"]] = count
        previous = {row["key"] for row in self.conn.execute(
            "SELECT key FROM alerts WHERE code=? AND state='open'", (code,))}
        for key, count in near_cap.items():
            msg = f"Gate has {count} dependencies; split it into parts."
            if key in previous:
                self._alerts_raise_finding(key, code, msg, now)
            else:
                self._finding(key, code, msg, now)
        for key in sorted(previous - near_cap.keys()):
            self._alerts_resolve(key, now, code=code)
        return near_cap.keys() != previous

    def _age_priorities(self, principal, now):
        """Drop an open, unclaimed 10 (after 3 days) or 9 (after 7) one level.

        The clock is the last ``item_created``/``priority_set`` event of the item (a
        ``claimed`` event since then means it was started in time):
        the router's re-confirm (``priority KEY <same value>``) writes one, and so
        does each drop, so a drop restarts the clock for the next one.  Every drop
        is a ``priority_set`` event carrying the reason.
        """
        out = []
        for item in self._all_items():
            days = AGING_DAYS.get(int(item.get("priority") or 0))
            if days is None or item["state"] != "open" or item.get("claimed_by"):
                continue
            row = self.conn.execute(
                "SELECT MAX(ts) FROM events WHERE key=? AND type IN"
                " ('item_created','priority_set')", (item["key"],)).fetchone()
            since = self._parse_iso(row[0]) if row and row[0] else None
            if since is None or now - since.timestamp() < days * 86400:
                continue
            if self.conn.execute(          # claimed since: it was started in time
                    "SELECT 1 FROM events WHERE key=? AND type='claimed' AND ts>=?"
                    " LIMIT 1", (item["key"], row[0])).fetchone():
                continue
            level = int(item["priority"])
            self._apply_priority(
                principal, item["key"], level - 1, now,
                reason=f"aged: priority {level} not claimed in {days} days since "
                       f"{row[0]}; re-confirm with `priority {item['key']} {level}`"
                       " to keep it")
            out.append({"key": item["key"], "from": level, "to": level - 1})
        return out
