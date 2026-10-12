"""PG-ANSWER-UI: the operator-only ``operator_answer`` verb for the questions panel.

``operator_answer(key, choice_id, text, user)`` answers one open operator item
from the UI and closes it.  Unlike the router's ``answer`` it needs no decision
row: the operator *is* the authority here, so the verb takes either the id of
one of the item's ``choices`` options or a free-text reply, never both:

* operator principal only (a run token is refused like every other identity);
* the item must be flagged ``operator``, be kind ``question``/``decision``/
  ``operator_task`` and be ``open`` — a claim, a review or a result is the
  lifecycle's answer, not the UI's;
* ``choice_id`` must name one of the item's options exactly, and free text
  obeys the same shape rules as ``answer`` at ``OPERATOR_ANSWER_CAP``;
* the same hash-chained ``answered`` event closes the item, attributed to the
  operator with ``source: "ui"`` and no ``decision_id``, so ``needs_you``,
  ``answers`` and the markdown export read it like any other answer.
"""
from __future__ import annotations

import re

from .credential_guard import guard
from .errors import Refused
from .item_fields import parse_choices

__all__ = ["OperatorAnswerMixin", "OPERATOR_ANSWER_CAP", "OPERATOR_ANSWER_KINDS"]

OPERATOR_ANSWER_CAP = 1000
OPERATOR_ANSWER_KINDS = ("question", "decision", "operator_task")
_USER = re.compile(r"[A-Za-z0-9._@-]{1,64}")


class OperatorAnswerMixin:
    """``operator_answer`` for :class:`plangraph.keeper.store.Store`."""

    def operator_answer(self, principal, key, choice_id=None, text=None, user=None):
        return self._run(principal, "operator_answer", key,
                         lambda now: self._operator_answer(principal, key,
                                                           choice_id, text,
                                                           user, now))

    def _check_operator_text(self, text):
        if not isinstance(text, str) or not text.strip():
            raise Refused("validation", "answer text must be a non-empty string")
        if len(text) > OPERATOR_ANSWER_CAP:
            raise Refused("validation",
                          f"answer text exceeds {OPERATOR_ANSWER_CAP} characters")
        guard("answer text", text)

    def _operator_target(self, key):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if not item["operator"] or item["kind"] not in OPERATOR_ANSWER_KINDS:
            raise Refused("validation",
                          f"{key} is not an open-answer {list(OPERATOR_ANSWER_KINDS)}"
                          " item for the operator")
        if item["state"] != "open":
            raise Refused("bad_state", f"{key} is {item['state']}, not open")
        return item

    def _operator_choice(self, item, choice_id):
        """The option *choice_id* names; the item must carry it as a real choice."""
        if choice_id is None:
            return None
        if not isinstance(choice_id, str):
            raise Refused("validation", "choice_id must be a choice id string")
        stored = item.get("choices")
        choices = stored if isinstance(stored, dict) else parse_choices(stored)
        options = (choices or {}).get("options") or []
        for option in options:
            if option.get("id") == choice_id:
                return option
        raise Refused("validation",
                      f"{choice_id!r} is not a choice of {item['key']}")

    def _operator_answer(self, principal, key, choice_id, text, user, now):
        self._require_role(principal, "operator")
        if (choice_id is None) == (text is None):
            raise Refused("validation",
                          "exactly one of choice_id and text must be given")
        if not isinstance(user, str) or not _USER.fullmatch(user):
            raise Refused("validation", "user must match [A-Za-z0-9._@-]{1,64}")
        if text is not None:
            self._check_operator_text(text)
        item = self._operator_target(key)
        option = self._operator_choice(item, choice_id)
        answer = option["label"] if option is not None else text
        with self._write():
            fresh = self._operator_target(key)
            if int(fresh["epoch"]) != int(item["epoch"]):
                raise Refused("conflict", f"{key} changed epoch under the transaction")
            # The cache row goes first, exactly like ``answer``: the event below
            # is what locks the key.
            cursor = self.conn.execute(
                "UPDATE items SET state='done', reviewer=?, updated_at=?"
                " WHERE key=? AND state='open' AND epoch=?",
                (principal.name, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, "open", item["epoch"])
            self._append(principal, key, "answered",
                         {"text": answer, "choice_id": choice_id, "source": "ui",
                          "user": user, "decision_id": None}, now)
        return {"key": key, "state": "done", "choice_id": choice_id, "source": "ui"}
