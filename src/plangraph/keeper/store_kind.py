"""PG-SET-KIND: the router turns an open ``lane`` into a ``question`` / ``operator_task``.

``set_kind(key, kind)`` is the one way to change an item's kind after creation
(``edit_definition`` cannot): a planned lane that turns out to wait on the
operator.  One hash-chained ``kind_set`` event ``{kind, was, operator, wait}``;
the cache row gets ``kind``, ``operator = 1`` and the kind's default ``wait``
(an explicit ``wait`` is kept), exactly what ``create_items`` would have stored
for that kind.  ``events.fold`` replays ``kind`` and ``wait`` from the event, so
``verify_integrity`` agrees with the cache.

Guards: router only; target kind ``question`` or ``operator_task`` only; the
item must be ``open`` and a ``lane`` today (no claim, review or result is ever
re-labelled, and the kind cannot flip back and forth).
"""
from __future__ import annotations

import contextlib
import re

from . import timing
from .errors import Refused
from .evidence_decisions import decision_book
from .item_fields import KIND_DEFAULTS

__all__ = ["KindMixin", "SETTABLE_KINDS"]

#: The only kinds ``set_kind`` may move a lane to.
SETTABLE_KINDS = ("question", "operator_task")


def _lifted_keys(decision):
    """The keys of a ``Lifts-Gate: k1 k2`` field: one ``;``-separated field of the decision cell.

    The field must open with the name, so prose such as "do not add Lifts-Gate: k" is no release.
    """
    keys = set()
    for field in decision.split(";"):
        name, sep, rest = field.strip().partition(":")
        if sep and name == "Lifts-Gate":
            keys.update(token for token in re.split(r"[ ,]+", rest.strip()) if token)
    return keys


@contextlib.contextmanager
def _no_snapshot():
    yield


class KindMixin:
    """Router-only ``set_kind``."""

    def set_kind(self, principal, key, kind):
        return self._run(principal, "set_kind", key,
                         lambda now: self._set_kind(principal, key, kind, now))

    def _set_kind(self, principal, key, kind, now):
        self._require_role(principal, "router")
        if kind not in SETTABLE_KINDS:
            raise Refused("validation", f"kind must be one of {list(SETTABLE_KINDS)}")
        # Read, assert and write inside one transaction: the item may have been
        # claimed or closed before the write lock, and then gets no event.
        with self._write():
            item = self._get_or_refuse(key)
            self._assert_unlocked(item)
            if item["state"] != "open":
                raise Refused("bad_state", f"{key} is {item['state']}, not open")
            if item["kind"] != "lane":
                raise Refused("validation",
                              f"{key} is a {item['kind']}; only a lane can change kind")
            wait = item["wait"] if item["wait"] is not None else KIND_DEFAULTS[kind][1]
            cursor = self.conn.execute(
                "UPDATE items SET kind=?, operator=1, wait=?, updated_at=?"
                " WHERE key=? AND state='open' AND kind='lane' AND epoch=?",
                (kind, wait, self._iso(now), key, item["epoch"]))
            self._row_guard(cursor, key, "open", item["epoch"])
            self._append(principal, key, "kind_set",
                         {"kind": kind, "was": "lane", "operator": True, "wait": wait}, now)
        return {"key": key, "kind": kind, "was": "lane"}

    def lift_operator_gate(self, principal, key, decision_id):
        return self._run(principal, "lift_operator_gate", key,
                         lambda now: self._lift_operator_gate(principal, key, decision_id, now))

    def _lift_operator_gate(self, principal, key, decision_id, now, proof=None):
        """Clear ``operator`` (and so ``waits_on_operator``) once the operator lifted the step.

        No edit, ``set_kind`` or owner change can clear the flag.  The router names the
        decision that lifted the gate: an OPERATOR row, not superseded by a later row,
        with a ``Lifts-Gate: <key> ...`` field; a row that only names the key may keep
        the gate closed and does not count.  The decision is read once, before either
        lock (``proof`` is that read when a batch made it already); the item and the
        principal are re-checked under the write.  One ``operator_gate_lifted`` event
        records the D-id and the actor.
        """
        self._require_role(principal, "router")
        if proof is None:
            proof = self._lift_proof(key, decision_id, now)
        with self._write():
            self._check_principal(principal)
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if not fresh["operator"]:
                raise Refused("bad_state", f"{key} has no operator gate to lift")
            cursor = self.conn.execute(
                "UPDATE items SET operator=0, updated_at=? WHERE key=? AND operator=1 AND epoch=?",
                (self._iso(now), key, fresh["epoch"]))
            self._row_guard(cursor, key, fresh["state"], fresh["epoch"])
            self._append(principal, key, "operator_gate_lifted",
                         {"decision_id": decision_id, "operator": False}, now)
        return {"key": key, "operator": False, "waits_on_operator": False,
                "decision_id": decision_id}

    def _lift_proof(self, key, decision_id, now):
        """Check the release decision; the one slow read of the decisions file, outside every lock."""
        from .store_runs import _operator_authority

        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "lift_operator_gate needs the operator decision id (D-n)")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        decisions = self._ctx(item, now).decisions
        fields = getattr(decisions, "fields", None)
        snapshot = getattr(decisions, "snapshot", None)
        try:
            with timing.outside_request_lock():
                with snapshot() if callable(snapshot) else _no_snapshot():
                    row = fields(decision_id) if callable(fields) else None
                    superseded = decision_book(decisions, fresh=True)["superseded"] if row else {}
        except Exception:  # noqa: BLE001 - an unreadable file fails closed
            row, superseded = None, {}
        if not row:
            raise Refused("evidence_unverified",
                          f"decision {decision_id} is missing, ambiguous or unreadable")
        if not _operator_authority(row):
            raise Refused("evidence_unverified", f"{decision_id} is not an OPERATOR decision")
        canonical = f"D-{int(decision_id[2:])}"
        if canonical in superseded:
            raise Refused("evidence_unverified",
                          f"{decision_id} is superseded by {superseded[canonical]}")
        if item["key"] not in _lifted_keys(row.get("decision", "")):
            raise Refused("evidence_unverified",
                          f"{decision_id} has no 'Lifts-Gate: {key}' release")
        return {"key": key, "decision_id": decision_id}
