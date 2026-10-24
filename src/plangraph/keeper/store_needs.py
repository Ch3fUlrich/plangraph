"""MK-READS: the ``needs_you`` and ``answers`` read verbs (thin doors over ``views``)."""
from __future__ import annotations

import json
import re

from .. import views

# Hyphens belong to identifiers, not standalone human-wait signals.
_OPERATOR = re.compile(r"(?<![\w-])(?:operator|OS-\d+)(?![\w-])", re.I)
_WAIT = re.compile(
    r"(?:^|[\n.;])[ \t]*(?:[A-Za-z][A-Za-z ]{0,19}:[ \t]*)?"
    r"(?:IDLE|waiting(?:_on| on))\b[^\n.;]*(?:" + _OPERATOR.pattern + ")", re.I)


class NeedsMixin:
    """Read-only; any authenticated principal may call them."""

    def needs_you(self, principal, project=None):
        return self._run(principal, "needs_you", None,
                         lambda now: self._needs_you(project, principal))

    def _needs_you(self, project, principal):
        # Readers retain the public read contract; scoped actors see only their projects.
        items = [it for it in self._all_items()
                 if principal.role in ("router", "reader", "system")
                 or it.get("project") in (principal.projects or [])]
        rows = views.needs_you(items, project=project)
        # Prefer genuine waits regardless of the order supplied by the view.
        human_keys = {row["key"] for row in rows
                      if row.get("kind") != "operator_wait_unlinked"}
        rows = [row for row in rows if row.get("kind") != "operator_wait_unlinked"
                or row["key"] not in human_keys]
        by_key = {item["key"]: item for item in items}
        for item in items:
            if item["state"] not in ("claimed", "in_review"):
                continue
            if project is not None and item.get("project") != project:
                continue
            text = "\n".join(str(item.get(field) or "") for field in
                            ("note", "description", "done_criteria"))
            waiting = _OPERATOR.search(str(item.get("waiting_on") or "")) or _WAIT.search(text)
            if not waiting:
                continue
            if any(by_key.get(dep, {}).get("operator")
                   and by_key[dep]["state"] == "open" for dep in item["depends_on"]):
                continue
            # Genuine human waits take precedence over the diagnostic flag.
            if any(row["key"] == item["key"] for row in rows):
                continue
            rows.append({"key": item["key"], "project": item.get("project"),
                         "title": item["title"], "state": item["state"],
                         "kind": "operator_wait_unlinked", "priority": item["priority"],
                         "wait": "task", "unblocks": 0, "choices": None})
        return rows

    def answers(self, principal, project=None):
        return self._run(principal, "answers", None,
                         lambda now: views.answers(self._with_answers(),
                                                   project=project))

    def answer_map(self):
        """``key -> {"text", "decision_id", "ts"}`` for every answered item.

        An ``answered`` event (MK-ANSWER) wins.  SYNC-MD-IMPORT: a done QUESTIONS
        row that md-sync closed by decision has no answer text in the keeper, so
        it gets "answered by D-x, D-y" from its decision evidence (``ts`` None:
        ``done_at`` stays the close time).
        """
        out = {}
        decisions = {}
        for row in self.conn.execute(
                "SELECT key, ref FROM evidence WHERE kind='decision' ORDER BY id"):
            try:
                ref = json.loads(row["ref"] or "{}")
            except ValueError:
                ref = {}
            did = ref.get("id") if isinstance(ref, dict) else None
            if isinstance(did, str) and did and did not in decisions.setdefault(row["key"], []):
                decisions[row["key"]].append(did)
        for row in self.conn.execute(
                "SELECT key FROM items WHERE state='done' AND track='QUESTIONS'"
                " AND kind NOT IN ('gate', 'goal')"):
            ids = decisions.get(row["key"])
            if ids:
                out[row["key"]] = {"text": "answered by " + ", ".join(ids),
                                   "decision_id": ids[0], "ts": None}
        for row in self.conn.execute(
                "SELECT key,ts,payload FROM events WHERE type='answered'"
                " ORDER BY seq"):
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            out[row["key"]] = {"ts": row["ts"], "text": payload.get("text"),
                               "decision_id": payload.get("decision_id")}
        return out

    def _with_answers(self):
        """Every item, with ``answer`` (and ``done_at``) joined from ``answer_map``."""
        latest = self.answer_map()
        done_at = {row["key"]: row["ts"] for row in self.conn.execute(
            "SELECT key, ts FROM events WHERE type IN ('closed_on_decision','review_passed')")}
        items = self._all_items()
        for item in items:
            hit = latest.get(item["key"])
            if hit is not None:
                item["answer"] = {"text": hit["text"],
                                  "decision_id": hit["decision_id"]}
                item["done_at"] = hit["ts"] or done_at.get(item["key"])
        return items
