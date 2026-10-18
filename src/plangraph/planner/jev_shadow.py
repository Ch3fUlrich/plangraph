"""Jev shadow runner (RT-P1-JEV-SHADOW): the guarded ask, the sinks and the outcome.

This module is the only place that *runs* Jev.  It joins three already-written
pieces:

* :mod:`plangraph.planner.jev_client` - config, the key resolution and ``ask``,
  which never raises and always returns one answer per question key;
* :mod:`plangraph.planner.jev_questions` - the pure state/question/rule builders;
* the keeper's ``Store.jev_record`` / ``Store.jev_outcome`` verbs.

HARD RULES this module exists to enforce:

* Jev NEVER changes a decision.  Both hooks return *rows*, never a route or a
  verdict, and both deep-copy their inputs, so Jev can never mutate the caller's
  card or route.
* A slow, failing or unavailable Jev NEVER blocks.  Every ask runs in a daemon
  thread bounded by a hard deadline (:func:`guarded`), a timeout is reported as
  ``timeout`` and an exception as ``error`` (class name only, never the message),
  and both hooks wrap their whole body so an internal bug returns ``[]`` / ``None``
  instead of raising.
* The flag defaults to ``off``: nothing runs, no thread starts, no request is made.

The key VALUE is never touched here - ``jev_client`` resolves it at call time and
keeps it out of every config, answer and reason - so no row, reason or log line
written by this module can hold it.

A ``row`` is exactly the body of ``Store.jev_record`` (see ``store_jev._RECORD_KEYS``);
``copy.deepcopy`` is used because a routed card can carry nested lists.
"""
from __future__ import annotations

import copy
import json
import threading
from collections.abc import Mapping
from datetime import datetime, timezone

from . import jev_client, jev_questions

__all__ = ["Sink", "store_sink", "guarded", "shadow_routing", "shadow_qc_accept",
           "qc_hook", "fill_outcome", "jsonl_sink"]

#: Cap on a row's ``reason`` before it is handed to a sink (``store_jev`` clips to 200 too).
_REASON_CAP = 200
#: Sentinel that distinguishes "no attempt given" from "attempt given as None".
_UNSET = object()


class Sink:
    """A Jev sink, by convention: ``callable(row: dict) -> bool`` that never raises.

    This class only documents the shape (protocol by convention); any callable
    satisfying it is a sink.  ``True`` means the row landed.  A sink swallows its
    own errors - the runners additionally wrap every sink call so a badly behaved
    sink cannot break a hook either.
    """

    def __call__(self, row: dict) -> bool:  # pragma: no cover - convention only
        raise NotImplementedError


def _now() -> str:
    """UTC timestamp as an ISO string ending in ``Z`` (twin of ``shadow._now``)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_wait(timeout_s) -> float:
    """A usable ``Thread.join`` timeout; non-positive, non-numeric or infinite -> 0.001."""
    try:
        wait = float(timeout_s)
    except (TypeError, ValueError):
        wait = 0.0
    if not wait > 0 or wait == float("inf"):
        wait = 0.001
    return wait


def guarded(fn, timeout_s):
    """Run ``fn()`` in a daemon thread with a hard deadline; never raises, never blocks.

    Returns ``("ok", value, "")`` on success, ``("timeout", None, "no answer within
    <n>s")`` when the thread is still running at the deadline (the thread is
    abandoned: it is a daemon, so it cannot keep the process alive), or
    ``("error", None, "<ExceptionClassName>")`` when ``fn`` raised - the message
    text is deliberately dropped so a key-bearing message can never leak.  The
    wall time is bounded by ``timeout_s`` plus a few milliseconds; a non-positive
    or non-numeric ``timeout_s`` behaves as 0.001.
    """
    wait = _positive_wait(timeout_s)
    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - a bounded worker must never escape
            box["error"] = type(exc).__name__

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(wait)
    if thread.is_alive():
        return ("timeout", None, "no answer within %gs" % wait)
    if "error" in box:
        return ("error", None, box["error"])
    return ("ok", box.get("value"), "")


def store_sink(store, principal) -> Sink:
    """A sink that writes each row with ``store.jev_record``; swallows every error.

    ``store.jev_record`` raises ``Refused`` for a principal that may not write
    (a reader, a worker) and ``Exception`` for a malformed body; both become
    ``False`` here, so a hook never breaks because the keeper refused a row.
    """
    def _sink(row: dict) -> bool:
        try:
            store.jev_record(principal, row)
        except Exception:  # noqa: BLE001 - a refused row is just a row that did not land
            return False
        return True

    return _sink


def _cfg_model(cfg, answer=None) -> str:
    """The row's model: the answer's own, else ``cfg.model``, else the empty string."""
    model = getattr(answer, "model", "") if answer is not None else ""
    if isinstance(model, str) and model:
        return model
    fallback = getattr(cfg, "model", "")
    return fallback if isinstance(fallback, str) else ""


def _rows_from(answers, kinds_to_rule, attempt, card_key, cfg) -> list[dict]:
    """One ``jev_record`` body per kind in ``kinds_to_rule`` (``{kind: rule_answer}``).

    Each row takes status/reason/model/latency from the matching
    :class:`jev_client.JevAnswer`; ``jev_answer`` is the answer's choice (nulled for
    a non-ok status, exactly as ``store_jev`` stores it), plus its probabilities and
    confidence.  A kind missing from ``answers`` (or answered with ``None``) is an
    ``error`` with reason ``"no answer"``.  ``reason`` is clipped to 200 characters.
    """
    rows: list[dict] = []
    for kind, rule_answer in kinds_to_rule.items():
        answer = answers.get(kind) if isinstance(answers, Mapping) else None
        if answer is None:
            status, reason = "error", "no answer"
            choice = probabilities = confidence = latency_ms = None
        else:
            status = getattr(answer, "status", None)
            if not isinstance(status, str) or not status:
                status = "error"
            reason = getattr(answer, "reason", "") or ""
            choice = getattr(answer, "choice", None)
            probabilities = getattr(answer, "probabilities", None)
            confidence = getattr(answer, "confidence", None)
            latency_ms = getattr(answer, "latency_ms", None)
        if not isinstance(reason, str):
            reason = str(reason)
        if status != "ok":
            choice = None       # a non-ok row never carries an answer (store_jev rule)
        rows.append({
            "card": card_key,
            "attempt": attempt,
            "kind": kind,
            "rule_answer": rule_answer,
            "jev_answer": choice,
            "jev_probabilities": probabilities,
            "jev_confidence": confidence,
            "status": status,
            "reason": reason[:_REASON_CAP],
            "jev_model": _cfg_model(cfg, answer),
            "latency_ms": latency_ms,
        })
    return rows


def _fallback_answers(kinds_to_rule, status, reason) -> dict:
    """The same status/reason on every kind, with no Jev answer (a guarded failure)."""
    reason = (reason if isinstance(reason, str) else str(reason))[:_REASON_CAP]
    return {kind: jev_client.JevAnswer(status=status, choice=None, probabilities=None,
                                       confidence=None, reason=reason, model="",
                                       latency_ms=None)
            for kind in kinds_to_rule}


def _ask_guarded(state, questions, cfg, env):
    """One guarded Jev request carrying every question at once.

    Returns ``(status, answers, reason)`` from :func:`guarded`; on a timeout or an
    error ``answers`` is ``None`` and the caller turns the status into one row per
    kind.  One request for all four routing questions means the total delay is
    bounded once, not once per question.
    """
    return guarded(lambda: jev_client.ask(state, questions, cfg, env=env),
                   _positive_wait(getattr(cfg, "timeout_s", 0.0)) + 0.5)


def _deliver(rows, sink) -> None:
    """Hand each row to ``sink``; a badly behaved sink is swallowed."""
    if sink is None:
        return
    for row in rows:
        try:
            sink(row)
        except Exception:  # noqa: BLE001 - a sink bug must not break a hook
            pass


def _recordable(key) -> bool:
    """True only for a non-empty string card key (a row cannot be recorded without one)."""
    return isinstance(key, str) and bool(key)


def shadow_routing(card, route, *, sink=None, attempt=None, cfg=None,
                   env=None) -> list[dict]:
    """Shadow the rules router: ask Jev the four routing questions and return rows.

    OFF by default: with ``cfg`` unset the mode comes from ``env`` and an ``off``
    mode returns ``[]`` immediately (no thread, no request).  Otherwise the state
    and questions are built from deep copies of ``card``/``route``, Jev is asked
    once under a hard deadline, and one row per kind is built in
    :data:`jev_questions.ROUTING_KINDS` order.  Every row known goes to ``sink``
    when a sink is given and the card key is a non-empty string.  The return value
    is the list of rows - never a route: the decision is unchanged.
    """
    try:
        cfg = cfg or jev_client.config_from_env(env)
        if not jev_client.enabled(cfg):
            return []
        card_copy = copy.deepcopy(card)
        route_copy = copy.deepcopy(route)
        kinds_to_rule = jev_questions.rule_answers(route_copy)
        state = jev_questions.routing_state(card_copy, route_copy)
        questions = jev_questions.routing_questions(card_copy)
        status, answers, reason = _ask_guarded(state, questions, cfg, env)
        if status != "ok":
            answers = _fallback_answers(kinds_to_rule, status, reason)
        key = card_copy.get("key") if isinstance(card_copy, Mapping) else None
        rows = _rows_from(answers, kinds_to_rule, attempt, key, cfg)
        if _recordable(key):
            _deliver(rows, sink)
        return rows
    except Exception:  # noqa: BLE001 - an internal bug must never break routing
        return []


def shadow_qc_accept(card, verdict, *, rule_accept=None, phase=None, sink=None,
                     attempt=None, cfg=None, env=None) -> dict | None:
    """Shadow the QC-accept decision: ask Jev whether to accept and return one row.

    Same guarantees as :func:`shadow_routing` (never raises, ``off`` -> ``None``
    immediately, deep-copied inputs, bounded by the same deadline).  ``rule_accept``
    as a ``bool`` is the rules' answer (``"accept"``/``"reject"``); otherwise it is
    derived from ``verdict`` via :func:`jev_questions.rule_qc_answer`.  ``card`` may
    be ``None`` (treated as an empty mapping).  Returns the single ``qc_accept`` row.
    """
    try:
        cfg = cfg or jev_client.config_from_env(env)
        if not jev_client.enabled(cfg):
            return None
        card_copy = copy.deepcopy(card) if card is not None else {}
        if not isinstance(card_copy, Mapping):
            card_copy = {}
        verdict_copy = copy.deepcopy(verdict)
        if isinstance(rule_accept, bool):
            rule_answer = "accept" if rule_accept else "reject"
        else:
            rule_answer = jev_questions.rule_qc_answer(verdict_copy)
        kinds_to_rule = {"qc_accept": rule_answer}
        state = jev_questions.qc_state(card_copy, verdict_copy, phase)
        questions = {"qc_accept": jev_questions.qc_question()}
        status, answers, reason = _ask_guarded(state, questions, cfg, env)
        if status != "ok":
            answers = _fallback_answers(kinds_to_rule, status, reason)
        key = card_copy.get("key")
        rows = _rows_from(answers, kinds_to_rule, attempt, key, cfg)
        if _recordable(key):
            _deliver(rows, sink)
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001 - an internal bug must never break the QC hook
        return None


def qc_hook(card, *, sink=None, attempt=None, cfg=None, env=None):
    """Build the time-bounded ``plan_actions(..., shadow=...)`` QC-accept hook.

    The returned ``hook(verdict, phase, key, plan)`` asks Jev the SAME accept/reject
    question as the rules, in shadow, through :func:`shadow_qc_accept` with
    ``rule_accept=bool(plan.get("pass"))`` (the plan's own answer), and returns
    ``None``: it is observation only, so it can never change the plan.

    ``card`` is used as given when it already carries a ``key``; when ``key`` is a
    non-empty string and the card has none it is added
    (``{**(card or {}), "key": key}``) so the row is recordable.  ``sink``,
    ``attempt``, ``cfg`` and ``env`` are forwarded unchanged.  The hook never raises,
    so a bad card, a failing Jev or a broken sink is swallowed and the caller's plan
    is untouched.  Typical use, beside the pure decision::

        plan_actions(verdict, "p1", key,
                     shadow=qc_hook(card, sink=store_sink(store, principal), attempt=n))
    """
    def hook(verdict, phase, key, plan) -> None:
        try:
            has_key = isinstance(card, Mapping) and bool(card.get("key"))
            card_with_key = ({**(card or {}), "key": key}
                             if isinstance(key, str) and key and not has_key
                             else card)
            shadow_qc_accept(card_with_key, verdict,
                             rule_accept=bool(plan.get("pass")),
                             phase=phase, sink=sink, attempt=attempt,
                             cfg=cfg, env=env)
        except Exception:  # noqa: BLE001 - the caller's plan must never break
            pass
        return None

    return hook


def fill_outcome(store, principal, card_key, outcome, *, attempt=_UNSET) -> int:
    """Fill the later outcome on Jev rows for ``card_key``; returns the rows updated.

    ``attempt`` is forwarded only when given: omit it to fill every attempt, pass it
    to fill one attempt (``None`` matches only the NULL-attempt rows, as
    ``store.jev_outcome`` defines it).  The first write wins inside the store, so a
    second call returns 0.  Named ``fill_outcome`` so it does not clash with
    :func:`plangraph.planner.shadow.record_outcome`.  Every error is swallowed:
    a store refusal or a bad body returns 0.  The disabled flag does not matter
    here (an outcome only fills rows that already exist).
    """
    try:
        body = {"card": card_key, "outcome": outcome}
        if attempt is not _UNSET:
            body["attempt"] = attempt
        result = store.jev_outcome(principal, body)
        updated = result.get("updated", 0) if isinstance(result, Mapping) else 0
        return updated if isinstance(updated, int) and not isinstance(updated, bool) else 0
    except Exception:  # noqa: BLE001 - an outcome is best-effort, never a crash
        return 0


def jsonl_sink(path) -> Sink:
    """A sink appending one JSON line ``{"v": 1, "event": "jev", "ts": ..., **row}``.

    UTF-8, one line per row.  A write failure (``OSError``) or an unserialisable
    row (``ValueError``/``TypeError``) is swallowed and returns ``False``.  ``path``
    ``None`` or empty yields a sink that always returns ``False`` (nothing to write).
    """
    try:
        target = str(path).strip() if path is not None else ""
    except Exception:  # noqa: BLE001 - an unprintable path is simply no path
        target = ""
    if not target:
        def _closed(row: dict) -> bool:  # noqa: ARG001 - signature is the contract
            return False
        return _closed

    def _sink(row: dict) -> bool:
        try:
            line = json.dumps({"v": 1, "event": "jev", "ts": _now(), **row},
                              default=repr)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, ValueError, TypeError):
            return False
        return True

    return _sink
