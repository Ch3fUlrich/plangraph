"""MK-ANSWER: the router-only ``answer`` verb for ``question`` / ``decision`` items.

``answer(key, text, decision_id)`` records the operator's answer to a
``question`` or ``decision`` item and closes it, the way ``close_on_decision``
closes a tier-``none`` lane:

* router principal only; the item must be kind ``question`` or ``decision`` and
  ``open``, ``claimed`` or ``in_review``;
* ``decision_id`` must be in the decisions file and its ``Closes:`` tokens must
  name *key* (a ``Closes:`` keyword with no valid token fails closed);
* the ``answered`` event {text <= ``ANSWER_CAP``, decision_id} then makes the item
  ``done`` with the same lock and attribution as ``closed_on_decision``.

As in ``close_on_decision`` the decisions file is read before the write lock and
again under it, so nothing can change between the check and the commit.
"""
from __future__ import annotations

from .credential_guard import guard
from .errors import Refused

ANSWER_CAP = 2000
ANSWER_KINDS = ("question", "decision")
_ACTIVE = ("open", "claimed", "in_review")


class AnswerMixin:
    """``answer`` for :class:`plangraph.keeper.store.Store`."""

    def answer(self, principal, key, text, decision_id):
        return self._run(principal, "answer", key,
                         lambda now: self._answer(principal, key, text,
                                                  decision_id, now))

    def _answer_target(self, key):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["kind"] not in ANSWER_KINDS:
            raise Refused("validation",
                          f"{key} is kind {item['kind']!r}; answer needs a"
                          f" {' or '.join(ANSWER_KINDS)}")
        if item["state"] not in _ACTIVE:
            raise Refused("bad_state", f"{key} is {item['state']}, not active")
        return item

    def _check_answer_decision(self, key, decision_id, item, now):
        decisions = self._decisions_adapter(item, now)
        try:
            recorded = decisions.has(decision_id)
            named = ({self.resolve_key(token) for token in decisions.closes(decision_id)}
                     if recorded else set())
        except Exception:  # noqa: BLE001 - unreadable file fails closed
            recorded, named = False, set()
        if not recorded:
            raise Refused("evidence_unverified",
                          f"decision {decision_id} is not in the decisions file")
        if key not in named:
            raise Refused("evidence_unverified",
                          f"decision {decision_id} does not name {key} in Closes:")

    def _answer(self, principal, key, text, decision_id, now):
        self._require_role(principal, "router")
        if not isinstance(text, str) or not text.strip():
            raise Refused("validation", "answer text must be a non-empty string")
        if len(text) > ANSWER_CAP:
            raise Refused("validation", f"answer text exceeds {ANSWER_CAP} characters")
        guard("answer text", text)
        if (not isinstance(decision_id, str) or not decision_id
                or len(decision_id) > 64 or decision_id != decision_id.strip()):
            raise Refused("validation", "decision_id must be a decision id string")
        item = self._answer_target(key)
        self._check_answer_decision(key, decision_id, item, now)
        with self._write():
            fresh = self._answer_target(key)
            if int(fresh["epoch"]) != int(item["epoch"]):
                raise Refused("conflict", f"{key} changed epoch under the transaction")
            self._check_answer_decision(key, decision_id, fresh, now)
            # The cache row goes first, exactly like ``close_on_decision``: the
            # event below is what locks the key.
            cursor = self.conn.execute(
                "UPDATE items SET state='done', reviewer=?, updated_at=?"
                " WHERE key=? AND state IN ('open','claimed','in_review')"
                " AND epoch=?",
                (principal.name, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, fresh["state"], item["epoch"])
            self._append(principal, key, "answered",
                         {"text": text, "decision_id": decision_id}, now)
        return {"key": key, "state": "done", "decision_id": decision_id}
