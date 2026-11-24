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


_KEY = re.compile(r"[^:@/\s,;]+:[^:@/\s,;]+")
_DID = re.compile(r"D-[0-9]+")
#: the router's standing attribution of a relayed operator answer, at the head of the DECISION cell
_RELAY = re.compile(r"(?:\([^)]*\)\s*)?OPERATOR \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z, relayed by \S")


def _releases(decision, key):
    """True when the cell affirmatively releases *key*: a ``Lifts-Gate`` list naming it, or the
    recorded statement ``DECISION: the operator step of <key> (...) is lifted``.  A cell that
    merely names the key ("keep <key> blocked") releases nothing."""
    statement = re.compile(rf"DECISION: the operator step of {re.escape(key)}(?: \([^)]*\))? is lifted(?!\w)")
    return key in _lifted_keys(decision) or statement.search(decision) is not None


def _field_values(decision, name):
    """The values of the ``;``-separated fields of a decision cell that open with ``name:``."""
    for field in decision.split(";"):
        head, sep, rest = field.strip().partition(":")
        if sep and head == name:
            yield rest.strip()


def _lifted_keys(decision):
    """The keys of a ``Lifts-Gate: k1 k2`` field.  The whole value must be a list of item keys:
    prose inside the field ("Do not lift k until reviewed") makes the field no release."""
    keys = set()
    for value in _field_values(decision, "Lifts-Gate"):
        tokens = [t for t in re.split(r"[ ,]+", value) if t]
        if tokens and all(_KEY.fullmatch(t) for t in tokens):
            keys.update(tokens)
    return keys


def _operator_answer_ref(decision):
    """The ``D-n`` of a single ``Operator-Answer: D-n`` field, else ``None``."""
    values = list(_field_values(decision, "Operator-Answer"))
    return values[0] if len(values) == 1 and _DID.fullmatch(values[0]) else None


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

    def _lift_operator_gate(self, principal, key, decision_id, now, snapshot=None):
        """Clear ``operator`` (and so ``waits_on_operator``) once the operator lifted the step.

        No edit, ``set_kind`` or owner change can clear the flag.  The router names the
        decision row that lifted the gate: a ``Lifts-Gate: <key> ...`` field whose whole
        value is a list of item keys, not superseded, from an OPERATOR row or with an
        ``Operator-Answer: D-n`` field naming a relayed operator answer (``_RELAY``
        attribution or BY OPERATOR) that is not superseded either and itself releases the
        key (a ``Lifts-Gate`` list or ``DECISION: the operator step of <key> ... is lifted``).  A row that only names
        the key may keep the gate closed and does not count.  The decisions are read once,
        before any lock (a batch reads them for all its lifts before its transaction;
        ``snapshot`` is that read); the check under the write is pure, and the item and
        the principal are re-checked there.  One ``operator_gate_lifted`` event records the
        D-id and the actor.
        """
        self._require_role(principal, "router")
        if not isinstance(decision_id, str) or not _DID.fullmatch(decision_id):
            raise Refused("validation", "lift_operator_gate needs the operator decision id (D-n)")
        if snapshot is None:
            snapshot = self._read_lift_decisions([decision_id])
        with self._write():
            self._check_principal(principal)
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            self._check_lift_release(fresh["key"], decision_id, snapshot)
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

    def _read_lift_decisions(self, decision_ids):
        """The one slow read of the decisions file, outside every lock (never inside a transaction).

        ``{D-n: {"row", "ref", "superseded"}}`` for each id (``ref``: the row an
        ``Operator-Answer`` field names); an unreadable file or an absent row is ``None``.
        """
        decisions = self._adapter("decisions")
        fields = getattr(decisions, "fields", None)
        snapshot = getattr(decisions, "snapshot", None)
        out = {}
        try:
            with timing.outside_request_lock():
                with snapshot() if callable(snapshot) else _no_snapshot():
                    superseded = decision_book(decisions, fresh=True)["superseded"]
                    for did in decision_ids:
                        row = fields(did) if callable(fields) else None
                        ref_id = _operator_answer_ref((row or {}).get("decision", ""))
                        out[did] = {"row": row or None, "superseded": superseded,
                                    "ref": (fields(ref_id) or None) if ref_id else None}
        except Exception:  # noqa: BLE001 - an unreadable file fails closed
            return {did: None for did in decision_ids}
        return out

    @staticmethod
    def _check_lift_release(key, decision_id, snapshot):
        """Pure check of the release against the pre-read decisions."""
        from .store_runs import _operator_authority

        read = (snapshot or {}).get(decision_id)
        if not read or not read["row"]:
            raise Refused("evidence_unverified",
                          f"decision {decision_id} is missing, ambiguous or unreadable")
        row, superseded = read["row"], read["superseded"]

        def live(did):
            canonical = f"D-{int(did[2:])}"
            if canonical in superseded:
                raise Refused("evidence_unverified", f"{did} is superseded by {superseded[canonical]}")

        live(decision_id)
        if key not in _lifted_keys(row.get("decision", "")):
            raise Refused("evidence_unverified",
                          f"{decision_id} has no 'Lifts-Gate: {key}' release")
        if _operator_authority(row):
            return
        ref_id = _operator_answer_ref(row.get("decision", ""))
        ref = read["ref"]
        if ref is None or not (_operator_authority(ref) or _RELAY.match(ref.get("decision", ""))):
            raise Refused("evidence_unverified",
                          f"{decision_id} is not an OPERATOR decision and names no relayed operator answer")
        live(ref_id)
        if not _releases(ref.get("decision", ""), key):
            raise Refused("evidence_unverified", f"{ref_id} does not release {key}")
