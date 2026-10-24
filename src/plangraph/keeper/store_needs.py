"""MK-READS: the ``needs_you`` and ``answers`` read verbs (thin doors over ``views``)."""
from __future__ import annotations

import json
import re

from .. import views

# One operator-token rule for both paths (D-2458/2): a wait names the operator
# ("operator", "operator-123") or an OS work key ("OS-12", "OS-123-B"), while a
# hyphen or underscore joined to anything but digits (operator-sync,
# team-operator-sync, operator_x) is an identifier and never a wait.  The
# ``waiting_on:`` form and the IDLE/prose form both read this one pattern, so
# they agree on every token; the rule is stated in CHANGELOG too.
_TOKEN = r"(?<![\w-])(?:operator(?:-\d+)?|OS-\d+(?:-[A-Za-z0-9]+)?)(?![\w-])"
_OPERATOR = re.compile(_TOKEN, re.I)
# Clause anchoring depends on the "\n" join of fields below.
# Only a negation that governs the wait itself suppresses the flag
# (D-2458/3): one that negates the waiting act in the clause ("not waiting
# on operator") or the necessity of the wait right after the target
# ("operator sign-off not required").  A negation of the awaited state or of
# urgency ("operator not confirmed", "not urgent yet") is still a wait, so the
# old guard that scanned the whole clause for no/not/without is gone.
_NEG_WAIT = r"(?![^\n.;]*\b(?:no|not|without|never)\b[ \t]+(?:\w+[ \t]+){0,2}waiting\b)"
_NEG_NECESSITY = (r"(?![^\n.;]{0,30}\b(?:not|n't)\b[ \t]+"
                  r"(?:be[ \t]+)?(?:required|needed)\b)")
_ARTICLE = r"(?:an?[ \t]+|the[ \t]+)?"
_WAIT = re.compile(
    r"(?:^|[\n.;])" + _NEG_WAIT
    + r"[-*>\[\]_~#\d. \t]{0,8}"
    + r"(?:[^\n.;:=]{1,40}[:=][ \t]*)?"
    + r"(?:waiting_on[ \t]*:[ \t]*(?:" + _OPERATOR.pattern + r")|"
    + r"IDLE\b"
    + r"(?=[^\n.;]{0,30}" + _TOKEN + r")"
    + r"[ \t]*(?:(?:until|for|on|from|by)\b[ \t]*|:[ \t]*)?" + _ARTICLE
    + _TOKEN + r"|"
    # "waiting on" is a verb phrase: it may sit mid-sentence, unlike the
    # status token IDLE, which stays clause-initial or right after a label.
    + r"[^\n.;]{0,40}?\bwaiting on\b"
    + r"(?=[^\n.;]{0,30}" + _TOKEN + r")"
    + r"[ \t]*" + _ARTICLE + _TOKEN + r")"
    + _NEG_NECESSITY, re.I)


class NeedsMixin:
    """Read-only; any authenticated principal may call them, across projects.

    As with ready/show, project is an explicit filter, not an authority scope.
    """

    def needs_you(self, principal, project=None):
        return self._run(principal, "needs_you", None,
                         lambda now: self._needs_you(project, principal))

    def _needs_you(self, project, principal):
        items = self._all_items()
        rows = views.needs_you(items, project=project)
        # D-2458/5, the single precedence rule: a genuine row for a key -- of
        # any kind, a question included -- beats the diagnostic flag.  It is
        # applied once here, to the rows the view supplied, and once below to
        # every flag this verb appends; page_needs_view.js repeats it as a
        # defensive copy.  A flag row never counts as a genuine row.
        genuine = {row.get("key") for row in rows
                   if row.get("key") is not None
                   and row.get("kind") != "operator_wait_unlinked"}
        rows = [row for row in rows if row.get("kind") != "operator_wait_unlinked"
                or row.get("key") not in genuine]
        by_key = {item["key"]: item for item in items}
        for item in items:
            if item["state"] not in ("claimed", "in_review"):
                continue
            if project is not None and item.get("project") != project:
                continue
            text = "\n".join(str(item.get(field) or "") for field in
                            ("note", "description", "done_criteria"))
            waiting = _WAIT.search(f"waiting_on: {item.get('waiting_on') or ''}") or _WAIT.search(text)
            if not waiting:
                continue
            if any(by_key.get(dep, {}).get("operator")
                   and by_key[dep]["state"] == "open" for dep in item["depends_on"]):
                continue
            # Genuine human waits take precedence over the diagnostic flag.
            if item["key"] in genuine:
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
