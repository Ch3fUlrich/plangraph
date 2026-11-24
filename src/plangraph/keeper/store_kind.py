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
from .item_fields import KIND_DEFAULTS, RELEASE, check_release_value

__all__ = ["KindMixin", "SETTABLE_KINDS"]

#: The only kinds ``set_kind`` may move a lane to.
SETTABLE_KINDS = ("question", "operator_task")


_DID = re.compile(r"D-[0-9]+")
#: the gate a lift opens, as the recorded release names it
GATE = "operator"


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

    def lift_operator_gate(self, principal, key, decision_id, decision=None):
        return self._run(principal, "lift_operator_gate", key,
                         lambda now: self._lift_operator_gate(principal, key, decision_id, decision, now))

    def _lift_operator_gate(self, principal, key, decision_id, decision, now, recorded=None):
        """Clear ``operator`` (and so ``waits_on_operator``) once the operator lifted the step.

        No edit, ``set_kind`` or owner change can clear the flag.  The router (the only role here)
        sends ``decision="release"`` and the id of a recorded decision row that carries the one
        structured fact of a release: the whole fields ``Decision-Kind: release``, ``Item: <this
        key>`` and ``Gate: operator`` (``DecisionsAdapter.release``), and is not retired by a later
        row (the index's supersede relation).  Every other row (a conditional approval, a withdrawal,
        an unrelated decision, another item) lifts nothing; the rest of its text is stored and never
        read.  The decision index is read once before any lock (a batch reads all its lifts' ids
        before its transaction; ``recorded`` is that read); under the write the item and the
        principal are re-checked.  One ``operator_gate_lifted`` event records the D-id, the kind
        and the actor.
        """
        self._require_role(principal, "router")
        check_release_value(decision)
        if not isinstance(decision_id, str) or not _DID.fullmatch(decision_id):
            raise Refused("validation", "lift_operator_gate needs the recorded decision id (D-n)")
        if recorded is None:
            recorded = self._read_lift_decisions([decision_id])
        self._check_release_row(key, decision_id, recorded.get(decision_id))
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
                         {"decision_id": decision_id, "decision": RELEASE, "operator": False}, now)
        return {"key": key, "operator": False, "waits_on_operator": False,
                "decision_id": decision_id, "decision": RELEASE}

    def _read_lift_decisions(self, decision_ids):
        """``{D-n: {"release": {...}, "retired_by": D-m|None}}``: the one slow read of the decision index,
        outside every lock (never inside a transaction); an unreadable file reads as ``None``."""
        decisions = self._adapter("decisions")
        release = getattr(decisions, "release", None)
        snapshot = getattr(decisions, "snapshot", None)
        try:
            with timing.outside_request_lock():
                with snapshot() if callable(snapshot) else _no_snapshot():
                    retired = decision_book(decisions, fresh=True)["superseded"]
                    return {did: {"release": release(did) if callable(release) else {},
                                  "retired_by": retired.get(f"D-{int(did[2:])}")}
                            for did in decision_ids}
        except Exception:  # noqa: BLE001 - an unreadable file fails closed
            return {did: None for did in decision_ids}

    @staticmethod
    def _check_release_row(key, decision_id, read):
        """Pure: does the recorded row release exactly this item's operator gate, whole field by whole field?"""
        if read is None:
            raise Refused("evidence_unverified", f"decision {decision_id} cannot be read from the decisions file")
        if read["retired_by"]:
            raise Refused("evidence_unverified", f"{decision_id} is retired by {read['retired_by']}")
        fact = read["release"]
        if (fact.get("kind"), fact.get("item"), fact.get("gate")) != (RELEASE, key, GATE):
            raise Refused("evidence_unverified",
                          f"{decision_id} records no release of {key}: it needs the whole fields"
                          f" 'Decision-Kind: {RELEASE}; Item: {key}; Gate: {GATE}' (nothing else in its text is read)")
