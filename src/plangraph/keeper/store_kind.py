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


_KEY = re.compile(r"[^:@/\s,;()]+:[^:@/\s,;()]+")
_DID = re.compile(r"D-[0-9]+")
_ISO_UTC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"


def _group_end(text, start):
    """Index just after the ``)`` that closes the ``(`` at ``text[start]``, nesting counted; ``None`` if open."""
    depth = 0
    for pos in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[pos], 0)
        if depth == 0:
            return pos + 1
    return None


def _skip_group(text, pos, lead=" "):
    """``pos`` past an optional ``lead`` + balanced ``( ... )`` group; ``None`` when the group never closes."""
    if text.startswith(lead + "(", pos):
        return _group_end(text, pos + len(lead))
    return pos


def _first_field_keys(cell):
    """The keys of the ``Lifts-Gate: k1 k2`` field a cell OPENS with; every token of the value must be an item key."""
    name, sep, rest = cell.strip().split(";")[0].partition(":")
    tokens = [t for t in re.split(r"[ ,]+", rest.strip()) if t]
    return set(tokens) if sep and name == "Lifts-Gate" and tokens and all(map(_KEY.fullmatch, tokens)) else set()


def _statement_keys(text):
    """``{key}`` when *text* is exactly the release statement ``the operator step of <key> [(...)] is lifted``
    followed by the end of the text or ``:``/``;``/``.``; anything else (a condition, a denial) is no release."""
    found = re.match(r"the operator step of (\S+)", text)
    if not found or not _KEY.fullmatch(found[1]):
        return set()
    pos = _skip_group(text, found.end())
    tail = re.compile(r" is lifted(?=$|[:;.])").match(text, pos) if pos is not None else None
    return {found[1]} if tail else set()


def _relay_release_keys(cell):
    """The released key of a relayed operator answer, read at fixed positions as a whole:

    ``[(stamp)] OPERATOR <UTC time>, relayed by <who> [(source)]: "<operator words>" DECISION: <statement>``.
    The quoted words are skipped as one unit, so a release phrase inside them, before ``DECISION:`` or
    in any other place is not the decision statement.
    """
    text = cell.strip()
    pos = _skip_group(text, 0, lead="")
    head = re.compile(rf"\s*OPERATOR {_ISO_UTC}, relayed by [\w.@-]+").match(text, pos) if pos is not None else None
    pos = _skip_group(text, head.end()) if head else None
    opening = re.compile(r':\s+"').match(text, pos) if pos is not None else None
    if not opening:
        return set()
    pos = opening.end()
    while pos < len(text) and text[pos] != '"':
        pos += 2 if text[pos] == "\\" else 1
    decision = re.compile(r'"\s+DECISION: ').match(text, pos)
    return _statement_keys(text[decision.end():]) if decision else set()


def _answer_releases(row, key):
    """Does the answer row behind ``Operator-Answer`` release *key*: a relayed operator answer, or a
    BY OPERATOR row that opens with the ``Lifts-Gate`` field."""
    from .store_runs import _operator_authority

    cell = row.get("decision", "")
    return key in _relay_release_keys(cell) or (_operator_authority(row) and key in _first_field_keys(cell))


def _field_values(decision, name):
    """The values of the ``;``-separated fields of a decision cell that open with ``name:``."""
    for field in decision.split(";"):
        head, sep, rest = field.strip().partition(":")
        if sep and head == name:
            yield rest.strip()


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
        decision row that lifted the gate; its cell OPENS with ``Lifts-Gate: <key> ...`` (the
        whole value a list of item keys) and the row is not superseded.  The row is BY OPERATOR,
        or a following ``Operator-Answer: D-n`` field links a relayed operator answer whose one
        decision statement releases the key:
        ``[(stamp)] OPERATOR <UTC time>, relayed by <who> [(source)]: "<words>" DECISION: the
        operator step of <key> [(qualifier)] is lifted`` + end, ``:``, ``;`` or ``.``.  The
        statement is read at those fixed positions as a whole (qualifiers by a balanced scan),
        so a quoted phrase, a denial, a condition or a phrase after other text is no release.
        The decisions are read once, before any lock (a batch reads them for all its lifts
        before its transaction; ``snapshot`` is that read); the check under the write is pure,
        and the item and the principal are re-checked there.  One ``operator_gate_lifted`` event
        records the
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
        cell = row.get("decision", "")
        if key not in _first_field_keys(cell):
            raise Refused("evidence_unverified",
                          f"{decision_id} does not open with 'Lifts-Gate: {key}'")
        if _operator_authority(row):
            return
        ref_id = _operator_answer_ref(cell)
        ref = read["ref"]
        if ref is None or not _answer_releases(ref, key):
            raise Refused("evidence_unverified",
                          f"{decision_id} is not an OPERATOR decision and its Operator-Answer"
                          f" {ref_id} is not a relayed operator answer that lifts {key}")
        live(ref_id)
