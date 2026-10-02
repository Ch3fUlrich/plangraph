"""Read verbs for :class:`plangraph.keeper.store.Store`.

Every read is a thin adapter over :mod:`plangraph.views` — the pure functions
are the single source of truth for graph maths (DRY).  Reads are audited like
writes, so "every attempt" in the contract includes a refused read.
"""
from __future__ import annotations

from .. import views
from .errors import Refused

_DONE = "done"
_ACTIVE = ("claimed", "in_review")


class ReadMixin:
    """Read-only verbs; any authenticated principal may call them."""

    def ready(self, principal, project=None, n=10):
        return self._run(principal, "ready", None,
                         lambda now: views.brief(self._all_items(),
                                                 project=project, n=n))

    def show(self, principal, key):
        return self._run(principal, "show", key, lambda now: self._show(key))

    def blocked(self, principal, project=None):
        return self._run(principal, "blocked", None,
                         lambda now: views.blocked(self._all_items(), project=project))

    def waves(self, principal):
        return self._run(principal, "waves", None,
                         lambda now: views.waves(self._all_items()))

    def critical_path(self, principal, project=None):
        return self._run(principal, "critical_path", None,
                         lambda now: views.critical_path(self._all_items(),
                                                         project=project))

    def percent(self, principal, by="estimate", project=None):
        return self._run(principal, "percent", None,
                         lambda now: views.percent_done(self._all_items(), by=by,
                                                        project=project))

    def whoami(self, principal):
        return self._run(principal, "whoami", None,
                         lambda now: {"name": principal.name, "role": principal.role,
                                      "projects": list(principal.projects or []),
                                      "family": principal.family})

    # -------------------------------------------------------------- show
    def _show(self, key):
        item = self._get_or_refuse(key)
        index = {it["key"]: it for it in self._all_items()}
        deps = [{"key": dep, "state": index.get(dep, {}).get("state")}
                for dep in item["depends_on"]]
        unblocks = sorted(
            other["key"] for other in index.values()
            if key in other["depends_on"] and other["state"] not in views.TERMINAL)
        evidence = [{"kind": record["kind"], "verified": record["verified"],
                     "at": record["at"], "detail": record["detail"],
                     "ref": record["ref"]} for record in self._evidence(key)]
        reviews = [{"reviewer": v.get("reviewer"), "family": v.get("family")}
                   for v in self._round_verdicts(item)]
        return {
            "key": key, "title": item["title"],
            "description": item["description"], "host": item["host"],
            "project": item["project"],
            "kind": item["kind"], "track": item["track"], "owner": item["owner"],
            "state": item["state"], "priority": item["priority"],
            "review_tier": item["review_tier"],
            "epoch": item["epoch"], "claimed_by": item["claimed_by"],
            "delegate": item["delegate"], "lease_until": item["lease_until"],
            "reviewer": item["reviewer"], "done_criteria": item["done_criteria"],
            "review_waiver": item.get("review_waiver"), "reviews": reviews,
            "corrects": item["corrects"], "depends_on": deps,
            "unblocks": unblocks, "evidence": evidence,
        }

    # ---------------------------------------------------------- snapshot
    def snapshot(self, principal):
        return self._run(principal, "snapshot", None,
                         lambda now: self._snapshot(now))

    def _snapshot(self, now):
        items = self._all_items()
        corrected = {}
        for item in items:
            if item["corrects"]:
                corrected.setdefault(item["corrects"], []).append(item["key"])
        out = []
        for item in items:
            reviews = [{"reviewer": v.get("reviewer"), "family": v.get("family")}
                       for v in self._round_verdicts(item)]
            out.append({
                "project": item["project"], "id": item["id"], "key": item["key"],
                "title": item["title"], "description": item["description"],
                "host": item["host"], "kind": item["kind"], "track": item["track"],
                "owner": item["owner"], "state": item["state"],
                "estimate_h": item["estimate_h"], "estimate_src": item["estimate_src"],
                "depends_on": item["depends_on"], "corrects": item["corrects"],
                "done_criteria": item["done_criteria"],
                "evidence_kinds": item["evidence_kinds"],
                "operator": item["operator"], "priority": item["priority"],
                "review_tier": item["review_tier"],
                "source": item["source"], "note": item["note"],
                "epoch": item["epoch"], "claimed_by": item["claimed_by"],
                "worker_run": None, "reviewer_run": None,
                "lease_until": item["lease_until"], "model": item["model"],
                "review_waiver": item.get("review_waiver"), "reviews": reviews,
                "evidence": [{"kind": r["kind"], "ref": r["ref"],
                              "verified": r["verified"], "at": r["at"]}
                             for r in self._evidence(item["key"])],
                "updated_at": item["updated_at"],
                "corrected_by": sorted(corrected.get(item["key"], [])),
            })
        head = self.conn.execute(
            "SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return {"generated": self._iso(now), "head": head[0] if head else None,
                "items": out,
                "aliases": {a["old"]: a["new"] for a in self._aliases()}}

    # --------------------------------------------------------------- alerts
    def alerts(self, principal, project=None):
        return self._run(principal, "alerts", None,
                         lambda now: self._alerts(now, project))

    def _alerts(self, now, project):
        items = self._all_items()
        scope = [it for it in items
                 if project is None or it["project"] == project]
        keys = {it["key"] for it in scope}
        ready_keys = {it["key"] for it in views.ready(items, project=project)}
        in_flight = any(it["state"] in _ACTIVE for it in scope)
        goal_open = any(it["kind"] == "goal" and it["state"] not in views.TERMINAL
                        for it in scope)
        stalled = bool(scope) and not ready_keys and not in_flight and goal_open

        lease_expiring = []
        review_overdue = []
        for item in scope:
            if item["state"] == "claimed" and item["lease_until"]:
                due = self._parse_iso(item["lease_until"])
                if due is not None and due.timestamp() <= now + self.lease_warn_s:
                    lease_expiring.append({"key": item["key"],
                                           "claimed_by": item["claimed_by"],
                                           "lease_until": item["lease_until"]})
            if item["state"] == "in_review" and item["updated_at"]:
                since = self._parse_iso(item["updated_at"])
                if since is not None and now - since.timestamp() > self.review_overdue_s:
                    review_overdue.append(item["key"])
        findings = [dict(row) for row in self.conn.execute(
            "SELECT ts,key,code,msg FROM audit_findings ORDER BY id")
            if row["key"] is None or row["key"] in keys]
        refusals = [{"key": row["key"], "count": row["n"]}
                    for row in self.conn.execute(
                        "SELECT key, COUNT(*) AS n FROM audit_log"
                        " WHERE ok=0 AND key IS NOT NULL"
                        " GROUP BY key HAVING COUNT(*) >= 2 ORDER BY key")
                    if row["key"] in keys]
        return {"lease_expiring": sorted(lease_expiring, key=lambda x: x["key"]),
                "review_overdue": sorted(review_overdue),
                "findings": findings, "refusals": refusals, "stalled": stalled,
                "warnings": views.warnings(scope)}

    # --------------------------------------------------------------- digest
    def digest(self, principal):
        return self._run(principal, "digest", None,
                         lambda now: self._digest(principal))

    def _digest(self, principal):
        self._require_role(principal, "router")
        items = self._all_items()
        counts = {}
        for item in items:
            bucket = counts.setdefault(item["project"],
                                       {"open": 0, "claimed": 0, "in_review": 0,
                                        "done": 0, "cancelled": 0})
            bucket[item["state"]] = bucket.get(item["state"], 0) + 1
        for name in counts:
            counts[name]["stalled"] = self._alerts(self.now(), name)["stalled"]
        done = sorted((it for it in items if it["state"] == _DONE),
                      key=lambda it: (it["updated_at"] or "", it["key"]))
        done = [{"key": it["key"], "title": it["title"],
                 "evidence_kinds": [r["kind"] for r in self._evidence(it["key"])]}
                for it in done[-10:]]
        waivers = []
        for it in items:
            if not it.get("review_waiver"):
                continue
            if it["state"] == _DONE:
                waivers.append({"key": it["key"],
                                "decision_id": it["review_waiver"]})
            elif it["state"] in ("claimed", "in_review"):
                waivers.append({"key": it["key"],
                                "decision_id": it["review_waiver"],
                                "state": it["state"]})
        waivers = sorted(waivers, key=lambda entry: entry["key"])
        return {"projects": counts, "done": done, "waivers": waivers}
