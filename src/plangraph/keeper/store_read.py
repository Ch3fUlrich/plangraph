"""Read verbs for :class:`plangraph.keeper.store.Store`.

Every read is a thin adapter over :mod:`plangraph.views` — the pure functions
are the single source of truth for graph maths (DRY).  Reads are audited like
writes, so "every attempt" in the contract includes a refused read.
"""
from __future__ import annotations

import json

from .. import views
from ..views_progress import progress
from . import jev_report
from .brief import build as _build_brief
from .errors import Refused
from .item_fields import MODEL_FIELDS, parse_choices, parse_kg_refs

#: Capabilities a client may check before relying on them (D-1498/D-2760).
KEEPER_FEATURES = ("apply_ops.dry_run", "apply_ops.strict_body")
_DONE = "done"
_ACTIVE = ("claimed", "in_review")
_MERGED_KINDS = ("commit+ci", "commit")
_TS_TYPES = ("review_passed", "closed_on_decision", "answered", "item_created")


def _decode_models(value):
    if value is None:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None
    return list(parsed) if isinstance(parsed, list) else None


def _decode_run_ids(value):
    if value is None:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []

_ACTIVITY_TYPES = ("item_created", "legacy_state_set", "closed_on_decision",
                   "answered", "cancelled", "claimed", "submitted", "review_passed",
                   "changes_requested")
_ACTIVITY_WINDOW_S = 31 * 86400
_ACTIVITY_CAP = 5000


class ReadMixin:
    """Read-only verbs; any authenticated principal may call them."""

    def ready(self, principal, project=None, n=10, owner=None, mine=False, agent=False):
        """D-953: ``mine`` = items owned by the caller; ``agent`` drops operator steps."""
        owner = principal.name if mine else owner
        return self._run(principal, "ready", None,
                         lambda now: views.brief(self._all_items(), project=project, n=n,
                                                 owner=owner, agent=bool(agent)))

    def show(self, principal, key, brief=False):
        if brief:
            return self._run(principal, "show", key, lambda now: self._brief(key))
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

    def progress(self, principal, project=None):
        """PROGRESS-PANEL: per-project / per-phase progress of the snapshot rows."""
        return self._run(principal, "progress", None,
                         lambda now: progress(self._snapshot(now)["items"], project=project))

    def whoami(self, principal):
        return self._run(principal, "whoami", None,
                         lambda now: {"name": principal.name, "role": principal.role,
                                      "projects": list(principal.projects or []),
                                      "family": principal.family,
                                      "features": list(KEEPER_FEATURES)})

    # -------------------------------------------------------------- show
    def _run_derived(self, key, item, evidence=None):
        """Read-time ``record_run`` fields plus done/reviewed/merged."""
        done_at, created_ts, reviewed = None, None, False
        for row in self.conn.execute(
                "SELECT type,ts FROM events WHERE key=? AND type IN"
                " ('review_passed','closed_on_decision','answered','item_created')"
                " ORDER BY seq", (key,)):
            if row["type"] == "item_created" and created_ts is None:
                created_ts = row["ts"]
            elif row["type"] == "review_passed":
                reviewed = True
                if done_at is None:
                    done_at = row["ts"]
            elif row["type"] in ("closed_on_decision", "answered") and done_at is None:
                done_at = row["ts"]
        rows = evidence if evidence is not None else self._evidence(key)
        merged = any(r.get("verified") and r.get("kind") in _MERGED_KINDS
                     for r in rows)
        created_at = item.get("created_at") or created_ts
        return {"writer_model": item.get("writer_model"),
                "seat_models": _decode_models(item.get("seat_models")),
                "run_ids": _decode_run_ids(item.get("run_ids")),
                "created_at": created_at, "done_at": done_at,
                "reviewed": reviewed, "merged": merged}

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
        attempts = [dict(row) for row in self.conn.execute(
            "SELECT n, exit_class, harness, model, started_at, ended_at,"
            " tokens_in, tokens_out, cost FROM attempts WHERE key=?"
            " ORDER BY n", (key,)).fetchall()]
        costs = [a["cost"] for a in attempts if a["cost"] is not None]
        attempt_cost = round(sum(costs), 6) if costs else None
        info = {
            "key": key, "title": item["title"],
            "description": item["description"], "host": item["host"],
            "wait": item.get("wait"),
            **{name: item.get(name) for name in MODEL_FIELDS},
            "choices": parse_choices(item.get("choices")),
            "kg_refs": parse_kg_refs(item.get("kg_refs")),
            "waits_on_operator": bool(item.get("operator"))
            and item.get("state") not in ("done", "cancelled"),
            "project": item["project"],
            "kind": item["kind"], "track": item["track"], "owner": item["owner"],
            "state": item["state"], "priority": item["priority"],
            "review_tier": item["review_tier"],
            "epoch": item["epoch"], "claimed_by": item["claimed_by"],
            "delegate": item["delegate"], "lease_until": item["lease_until"],
            "reviewer": item["reviewer"], "done_criteria": item["done_criteria"],
            "review_waiver": item.get("review_waiver"), "reviews": reviews,
            "attempts": attempts, "attempt_cost": attempt_cost,
            "corrects": item["corrects"], "fixes": item.get("fixes"),
            "fixed_by": sorted(o["key"] for o in index.values()
                               if o.get("fixes") == key),
            "depends_on": deps,
            "unblocks": unblocks, "evidence": evidence,
        }
        # RT-P1-JEV-SHADOW: the Jev shadow rows, only when the card has any, so
        # a card without them keeps its exact previous show output (the keys are
        # optional in the frozen Show schema).
        rows = self.jev_rows(key)
        if rows:
            info["jev_shadow"] = [jev_report.row_view(r) for r in rows]
            info["jev_agreement"] = jev_report.agreement_report(rows)["overall"]
        # PG-EFFICIENCY-GATES: review rounds, fix log, design -- only when there
        # is any, so an item without them keeps its exact previous show output.
        gates = self.gates_summary(key)
        if gates["rounds"] or gates["fix_log"]:
            info["gates"] = gates
        info.update(self._run_derived(key, item))
        return info

    def _brief(self, key):
        """The short task brief: identity, state, requirements, latest note."""
        item = self._get_or_refuse(key)
        index = {it["key"]: it for it in self._all_items()}
        deps = [{"key": dep, "state": index.get(dep, {}).get("state")}
                for dep in item.get("depends_on") or []]
        return _build_brief(item, deps)

    # ---------------------------------------------------------- snapshot
    def snapshot(self, principal):
        return self._run(principal, "snapshot", None,
                         lambda now: self._snapshot(now))

    def _snapshot_run_meta(self):
        """One grouped query for done/reviewed/created timestamps + merged."""
        created, done, reviewed = {}, {}, set()
        for row in self.conn.execute(
                "SELECT key,type,ts FROM events WHERE type IN"
                " ('review_passed','closed_on_decision','answered','item_created')"
                " ORDER BY seq"):
            if row["type"] == "item_created":
                created.setdefault(row["key"], row["ts"])
            elif row["type"] == "review_passed":
                reviewed.add(row["key"])
                done.setdefault(row["key"], row["ts"])
            else:
                done.setdefault(row["key"], row["ts"])
        merged = {row["key"] for row in self.conn.execute(
            "SELECT key FROM evidence WHERE verified!=0 AND kind IN"
            " ('commit+ci','commit') GROUP BY key")}
        return created, done, reviewed, merged

    def _snapshot(self, now):
        items = self._all_items()
        corrected, fixed = {}, {}
        for item in items:
            if item["corrects"]:
                corrected.setdefault(item["corrects"], []).append(item["key"])
            if item.get("fixes"):
                fixed.setdefault(item["fixes"], []).append(item["key"])
        created_ts, done_ts, reviewed_keys, merged_keys = self._snapshot_run_meta()
        answers = self.answer_map()   # SYNC-MD-IMPORT: the page's Answers list
        evidence = self._evidence_by_key()      # set-based: one query each, not one per item
        verdicts = self._verdicts_by_key()
        out = []
        for item in items:
            reviews = [{"reviewer": v.get("reviewer"), "family": v.get("family")}
                       for v in self._current_round(item, verdicts.get(item["key"], []))]
            out.append({
                "project": item["project"], "id": item["id"], "key": item["key"],
                "title": item["title"], "description": item["description"],
                "host": item["host"], "wait": item.get("wait"),
                **{name: item.get(name) for name in MODEL_FIELDS},
                "choices": parse_choices(item.get("choices")),
                "kg_refs": parse_kg_refs(item.get("kg_refs")),
            "kg_refs": parse_kg_refs(item.get("kg_refs")),
                "waits_on_operator": bool(item.get("operator"))
                and item.get("state") not in ("done", "cancelled"),
                "kind": item["kind"], "track": item["track"],
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
                             for r in evidence.get(item["key"], [])],
                "updated_at": item["updated_at"],
                "corrected_by": sorted(corrected.get(item["key"], [])),
                "fixes": item.get("fixes"),
                "fixed_by": sorted(fixed.get(item["key"], [])),
                "writer_model": item.get("writer_model"),
                "seat_models": _decode_models(item.get("seat_models")),
                "run_ids": _decode_run_ids(item.get("run_ids")),
                "created_at": item.get("created_at")
                or created_ts.get(item["key"]),
                "done_at": done_ts.get(item["key"]),
                "reviewed": item["key"] in reviewed_keys,
                "merged": item["key"] in merged_keys,
                "answer": ({"text": answers[item["key"]]["text"],
                            "decision_id": answers[item["key"]]["decision_id"]}
                           if item["key"] in answers else None),
            })
        head = self.conn.execute(
            "SELECT hash, seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return {"generated": self._iso(now), "head": head[0] if head else None,
                "head_seq": head[1] if head else 0,   # PG-LOCAL-COPY: readers refuse an older copy
                "items": out,
                "aliases": {a["old"]: a["new"] for a in self._aliases()},
                "activity": self._activity(now),
                "decision_text": self._decision_text(items, now, evidence),
                "progress": progress(out)}   # PROGRESS-PANEL: the page's static panel

    def _activity(self, now):
        """Compact ``events`` feed for the last 31 days, oldest first."""
        try:
            cutoff = self._iso(float(now) - _ACTIVITY_WINDOW_S)
            placeholders = ",".join("?" for _ in _ACTIVITY_TYPES)
            rows = self.conn.execute(
                "SELECT ts, key, type, actor, payload FROM events"
                f" WHERE type IN ({placeholders}) AND ts >= ?"
                " ORDER BY seq DESC LIMIT ?",
                (*_ACTIVITY_TYPES, cutoff, _ACTIVITY_CAP)).fetchall()
            out = []
            for row in reversed(list(rows)):
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (ValueError, TypeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                entry = {"ts": row["ts"], "key": row["key"],
                         "type": row["type"], "actor": row["actor"]}
                state = payload.get("state")
                if isinstance(state, str) and state:
                    entry["state"] = state
                if row["type"] == "cancelled":
                    reason = payload.get("reason")
                    if isinstance(reason, str) and reason:
                        entry["reason"] = reason[:120]
                out.append(entry)
            return out
        except Exception:  # noqa: BLE001 - snapshot never raises
            return []

    def _decision_text(self, items, now, evidence=None):
        """Map of decision id -> cell text for ids cited by item evidence."""
        try:
            wanted: list[str] = []
            seen: set[str] = set()
            for item in items:
                records = (evidence.get(item["key"], []) if evidence is not None
                           else self._evidence(item["key"]))
                for record in records:
                    if record.get("kind") != "decision":
                        continue
                    ref = record.get("ref") or {}
                    did = ref.get("id") if isinstance(ref, dict) else None
                    if isinstance(did, str) and did and did not in seen:
                        seen.add(did)
                        wanted.append(did)
            if not wanted or not items:
                return {}
            try:
                adapter = self._ctx(items[0], now).decisions
            except Exception:  # noqa: BLE001 - snapshot never raises
                return {}
            text_fn = getattr(adapter, "text", None)
            if not callable(text_fn):
                return {}
            result: dict[str, str] = {}
            for did in wanted:
                try:
                    value = text_fn(did)
                except Exception:  # noqa: BLE001 - one bad row skips
                    continue
                if not isinstance(value, str) or not value:
                    continue
                result[did] = value[:600]
            return result
        except Exception:  # noqa: BLE001 - snapshot never raises
            return {}

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
