"""Jev shadow verbs: the free model's answers and their later outcome.

``jev_shadow`` (schema v13) keeps one row per card/attempt: the rules-router's
answer, the free Jev model's answer, and -- later -- the rules-router's verdict
on the same question.  DESIGN DECISION: this table is NOT append-only.  Exactly
one column is filled in later, ``outcome`` (with ``outcome_at``), and the fill is
FIRST WRITE WINS (``UPDATE ... WHERE outcome IS NULL``).  No other column is
ever updated and rows are never deleted, so a row is an immutable record of what
the rules and Jev answered plus the one mutable verdict about it.

Roles: ``router``, ``system`` and ``l1`` (only inside its own projects, via
``_may_act_in``) may write; ``worker``, ``spawner``, ``reviewer`` and ``reader``
are refused for writes.  The reads (``jev_rows`` and ``jev_report``) are open to
any authenticated principal.  The keeper's own observe-only rows - a verdict's
``qc_accept`` and a worker verb's outcome fill - never write as their caller:
they go through :func:`_jev_writer`, which keeps the caller when its role may
write and otherwise files under the synthetic ``system`` identity, so a fill is
never a swallowed refusal and never a raise.

The report numbers come from the pure module
``plangraph.keeper.jev_report``; this module never restates them.

All SQL is portable (``?`` placeholders, ids assigned as ``MAX(id)+1`` inside the
write lock) so it runs unchanged on SQLite and Postgres.
"""
from __future__ import annotations

import json
import math
import re

from . import auth, jev_report
from .errors import Refused
from .schema import JEV_KINDS, JEV_STATUSES
from .store_cards import _text
from .store_worker import _card_name, _check_body

__all__ = ["JevMixin"]

_WRITE_ROLES = ("router", "system", "l1")
_TEXT_CAP = 200
_MODEL_CAP = 100
_PROB_CAP = 255
_PROB_NAME_CAP = 200
_DECISION_ID_CAP = 64
_INPUT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_RECORD_KEYS = frozenset((
    "card", "attempt", "decision_id", "input_hash", "kind", "rule_answer",
    "jev_answer", "jev_probabilities", "jev_confidence", "status", "reason",
    "jev_model", "latency_ms"))
_OUTCOME_KEYS = frozenset(("card", "attempt", "kind", "outcome"))


def _decision_id(value):
    """An optional decision identifier; refuse non-strings and > 64 chars."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str) or len(value) > _DECISION_ID_CAP:
        raise Refused("validation",
                      f"decision_id must be a string of at most {_DECISION_ID_CAP} characters or null")
    return value


def _input_hash(value):
    """An optional SHA-256 hash of the input state; refuse bad shapes."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, str)
            or not _INPUT_HASH_RE.match(value)):
        raise Refused("validation",
                      "input_hash must be a 64-character lowercase hex string or null")
    return value


def _positive_attempt(value):
    """A 1-based attempt number, or None; refuse 0, ``True`` and non-ints."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Refused("validation", "attempt must be a positive integer or null")
    return value


def _probabilities_text(value):
    """Canonical JSON text for a probability map, or None; refuse bad shapes."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Refused("validation", "jev_probabilities must be an object or null")
    if len(value) > _PROB_CAP:
        raise Refused("validation",
                      f"jev_probabilities has more than {_PROB_CAP} entries")
    clean = {}
    for name, prob in value.items():
        if not isinstance(name, str) or not name or len(name) > _PROB_NAME_CAP:
            raise Refused("validation", "jev_probabilities keys must be non-empty"
                          f" strings of at most {_PROB_NAME_CAP} characters")
        if (isinstance(prob, bool) or not isinstance(prob, (int, float))
                or not math.isfinite(prob) or not 0 <= float(prob) <= 1):
            raise Refused("validation",
                          "jev_probabilities values must be numbers in [0, 1]")
        clean[name] = float(prob)
    return json.dumps(clean, sort_keys=True)


def _confidence(value):
    """A finite confidence as a float, or None; refuse bool and bad numbers."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise Refused("validation", "jev_confidence must be a finite number or null")
    return float(value)


def _jev_writer(principal):
    """Who files an observe-only Jev row: *principal* when it may write, else ``system``.

    The one home for this mapping, shared by both fill paths: the verdict's
    ``qc_accept`` row (``store_review``) and the worker verbs' outcome fill
    (``store_worker``).  A ``verdict`` can only come from a ``reviewer`` and a
    ``worker_park``/``worker_complete`` from a ``worker``, while
    ``_WRITE_ROLES`` refuses both (worker, spawner, reviewer and reader never
    write), so passing the caller through would turn every row into a swallowed
    refusal and neither verdicts nor attempt outcomes would ever be shadowed.

    The row is the keeper's own observation of the event, not the caller's own
    write, so it is filed under the synthetic ``system`` identity the store
    already accepts (the same one the API reaper and the projector use).  Every
    other role - including one no verb could ever hand here - maps to that same
    identity, so this never raises.
    """
    if getattr(principal, "role", None) in _WRITE_ROLES:
        return principal
    return auth.Principal("system", "system", [], None)


class JevMixin:
    """The Jev shadow verbs for :class:`plangraph.keeper.store.Store`."""

    # ------------------------------------------------------------- helpers
    def _jev_scoped(self, principal, card):
        """Resolve *card* to its live key, refuse if unknown, check scope.

        The same three steps as ``WorkerMixin._resolve_scoped``; copied here so
        this mixin does not depend on the worker one.
        """
        key = self.resolve_key(card)
        item = self._get_or_refuse(key)
        self._may_act_in(principal, item["project"])
        return key

    # -------------------------------------------------------------- record
    def jev_record(self, principal, body):
        return self._run(principal, "jev_record", _card_name(body),
                         lambda now: self._jev_record(principal, body, now))

    def _jev_record(self, principal, body, now):
        # The role is checked FIRST, so a reader gets 403 rather than 400 for a
        # malformed body.
        self._require_role(principal, *_WRITE_ROLES)
        _check_body(body, _RECORD_KEYS, "jev_record")
        card = _text("card", body.get("card"), required=True)
        key = self._jev_scoped(principal, card)
        attempt = _positive_attempt(body.get("attempt"))
        decision_id = _decision_id(body.get("decision_id"))
        input_hash = _input_hash(body.get("input_hash"))
        kind = body.get("kind")
        if kind not in JEV_KINDS:
            raise Refused("validation", f"kind must be one of {list(JEV_KINDS)}")
        status = body.get("status")
        if status not in JEV_STATUSES:
            raise Refused("validation",
                          f"status must be one of {list(JEV_STATUSES)}")
        rule = body.get("rule_answer")
        if isinstance(rule, str):
            rule = rule[:_TEXT_CAP]     # an over-long answer is clipped, not refused
        rule = _text("rule_answer", rule, required=True, cap=_TEXT_CAP, key=key)
        answer = body.get("jev_answer")
        if answer is not None:
            if not isinstance(answer, str) or not answer.strip():
                raise Refused("validation",
                              "jev_answer must be a non-empty string or null")
            answer = _text("jev_answer", answer[:_TEXT_CAP], cap=_TEXT_CAP, key=key)
        if status == "ok":
            if answer is None:
                raise Refused("validation", "status ok requires a jev_answer")
        else:
            answer = None    # a non-ok row never carries an answer
        probs = _probabilities_text(body.get("jev_probabilities"))
        confidence = _confidence(body.get("jev_confidence"))
        reason = body.get("reason")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise Refused("validation", "reason must be a string")
        reason = _text("reason", reason[:_TEXT_CAP], cap=_TEXT_CAP, key=key) or ""
        model = body.get("jev_model")
        if model is None:
            model = ""
        if not isinstance(model, str):
            raise Refused("validation", "jev_model must be a string")
        model = _text("jev_model", model[:_MODEL_CAP], cap=_MODEL_CAP, key=key) or ""
        latency = body.get("latency_ms")
        if latency is not None and (isinstance(latency, bool)
                                    or not isinstance(latency, int)
                                    or latency < 0):
            raise Refused("validation",
                          "latency_ms must be a non-negative integer or null")
        with self._write():
            row = self.conn.execute(
                "INSERT INTO jev_shadow(id,key,attempt,decision_id,input_hash,kind,"
                "rule_answer,jev_answer,jev_probabilities,jev_confidence,status,"
                "reason,outcome,jev_model,latency_ms,created_at,outcome_at)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM jev_shadow),"
                "?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL) RETURNING id",
                (key, attempt, decision_id, input_hash, kind, rule, answer, probs,
                 confidence, status, reason, model, latency, self._iso(now))).fetchone()
        return {"id": row[0], "card": key, "kind": kind}

    # ------------------------------------------------------------- outcome
    def jev_outcome(self, principal, body):
        return self._run(principal, "jev_outcome", _card_name(body),
                         lambda now: self._jev_outcome(principal, body, now))

    def _jev_outcome(self, principal, body, now):
        self._require_role(principal, *_WRITE_ROLES)
        _check_body(body, _OUTCOME_KEYS, "jev_outcome")
        card = _text("card", body.get("card"), required=True)
        key = self._jev_scoped(principal, card)
        outcome = body.get("outcome")
        if isinstance(outcome, str):
            outcome = outcome[:_TEXT_CAP]   # clipped, not refused
        outcome = _text("outcome", outcome, required=True, cap=_TEXT_CAP, key=key)
        kind = body.get("kind")
        if kind is not None and kind not in JEV_KINDS:
            raise Refused("validation", f"kind must be one of {list(JEV_KINDS)}")
        # The UPDATE is built from fixed fragments only; values are parameters.
        where = "key=? AND outcome IS NULL"
        params = [key]
        if "attempt" in body:
            attempt = _positive_attempt(body.get("attempt"))
            if attempt is None:
                where += " AND attempt IS NULL"     # match only NULL attempts
            else:
                where += " AND attempt=?"
                params.append(attempt)
        if kind is not None:
            where += " AND kind=?"
            params.append(kind)
        # The row count is taken portably: select the matching ids, update each
        # one guarded by ``outcome IS NULL`` and sum the rowcounts ourselves.
        updated = 0
        with self._write():
            ids = [row[0] for row in self.conn.execute(
                f"SELECT id FROM jev_shadow WHERE {where}", tuple(params))]
            for row_id in ids:
                cursor = self.conn.execute(
                    "UPDATE jev_shadow SET outcome=?, outcome_at=?"
                    " WHERE id=? AND outcome IS NULL",
                    (outcome, self._iso(now), row_id))
                updated += cursor.rowcount
        return {"card": key, "updated": updated}

    # ---------------------------------------------------------------- reads
    def jev_rows(self, key):
        """Every ``jev_shadow`` row for *key*, oldest first (no audit, no role)."""
        rows = self.conn.execute(
            "SELECT id,key,attempt,decision_id,input_hash,kind,rule_answer,"
            "jev_answer,jev_probabilities,jev_confidence,status,reason,outcome,"
            "jev_model,latency_ms,created_at,outcome_at FROM jev_shadow WHERE key=? ORDER BY id",
            (key,)).fetchall()
        return [dict(row) for row in rows]

    def jev_report(self, principal, project=None):
        # The guard lives INSIDE _run (D-2458): the token re-check and the
        # denial audit run before an out-of-scope project can refuse.
        return self._run(principal, "jev_report", None,
                         lambda now: self._jev_report_in(principal, project))

    def _jev_report_in(self, principal, project):
        if isinstance(principal, auth.OversightPrincipal) and project is not None:
            self._require_project(principal, project)
        return self._jev_report(project)

    def _jev_report(self, project):
        rows = [dict(row) for row in self.conn.execute(
            "SELECT kind,rule_answer,jev_answer,status,reason,outcome,key"
            " FROM jev_shadow")]
        if project is not None:
            # ``key`` is ``project:id``; keep only the requested project.
            rows = [row for row in rows
                    if (row.get("key") or "").split(":", 1)[0] == project]
        return jev_report.agreement_report(rows)
