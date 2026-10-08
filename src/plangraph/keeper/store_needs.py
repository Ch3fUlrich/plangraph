"""MK-READS: the ``needs_you`` and ``answers`` read verbs (thin doors over ``views``)."""
from __future__ import annotations

import json

from .. import views


class NeedsMixin:
    """Read-only; any authenticated principal may call them."""

    def needs_you(self, principal, project=None):
        return self._run(principal, "needs_you", None,
                         lambda now: views.needs_you(self._all_items(),
                                                     project=project))

    def answers(self, principal, project=None):
        return self._run(principal, "answers", None,
                         lambda now: views.answers(self._with_answers(),
                                                   project=project))

    def _with_answers(self):
        """Every item, with ``answer`` / ``done_at`` joined from its ``answered`` event."""
        latest = {}
        for row in self.conn.execute(
                "SELECT key,ts,payload FROM events WHERE type='answered'"
                " ORDER BY seq"):
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            latest[row["key"]] = {"ts": row["ts"], "text": payload.get("text"),
                                  "decision_id": payload.get("decision_id")}
        items = self._all_items()
        for item in items:
            hit = latest.get(item["key"])
            if hit is not None:
                item["answer"] = {"text": hit["text"],
                                  "decision_id": hit["decision_id"]}
                item["done_at"] = hit["ts"]
        return items
