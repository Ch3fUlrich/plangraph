"""Jev shadow hook (design D18): an audit-only shadow answerer beside the rules router.

``route_with_shadow`` always routes with :func:`plangraph.planner.routing.route_card`
first and returns *that* route unchanged: the shadow answer is an observation for the
audit log, never a decision.  The legacy hook is off unless
``PLANGRAPH_PLANNER_SHADOW`` reads ``1``/``true``/``yes``/``on``; it needs both an
``answerer`` and a log path (argument or ``PLANGRAPH_PLANNER_SHADOW_LOG``).  Each run
appends one JSONL ``decision`` row (rules, shadow answer, shadow error, first-leg
agreement) and ``record_outcome`` later appends the attempt outcome for the same id;
``read_rows`` / ``summarize`` read them back.

The **Jev hook** (RT-P1-JEV-SHADOW) runs beside that legacy one, on its OWN switch:
``PLANGRAPH_JEV=off|stub|api`` (default ``off``), driven by
:mod:`plangraph.planner.jev_shadow`.  The routing hook here (``route_with_shadow``)
is the Jev call site for routing, and the second call site is the QC hook
:func:`qc_with_shadow`, called from the review accept decision in
``review.actions.plan_actions``.  It asks the free Jev decision model the four
routing questions (harness, window class, attempt budget, review level) in shadow and
sends the rows to a sink: the keeper store when ``store``/``principal`` are given, else
the same JSONL log (rows with ``event="jev"``).  Both hooks run regardless of
``PLANGRAPH_PLANNER_SHADOW`` and are a cheap no-op when Jev is off.  ``record_outcome``
also fills the keeper outcome for the card when a ``store``, a ``principal`` and a
``card_key`` are given.

The legacy JSONL ``decision`` row and the returned ``decision_id`` are unchanged, and
neither the route nor the id ever depends on Jev: the decision is the rules' alone.
``summarize`` reports the legacy rows and ``summarize_jev`` the Jev agreement via
:mod:`plangraph.keeper.jev_report`, whose keeper-side equivalent is
``GET /v1/jev_report``.  ``main`` renders both from a log:
``python3 -m plangraph.planner.shadow report [--log PATH] [--json]``.

Failures are ignored by design: an answerer exception becomes ``shadow_error``, a log
write failure (``OSError``) is swallowed, and nothing about the returned route changes.
Only ``card_key`` and rule/shadow/outcome fields are logged - never card text such as
``heldout_check``, ``check_cmd`` or ``write_set``.  Stdlib plus the two in-repo modules
above: no schema change, and no network call here (the Jev client makes any call,
guarded, itself).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from plangraph.keeper import jev_report

from . import jev_shadow
from .routing import Route, route_card

__all__ = ["ENV_FLAG", "ENV_LOG", "Answerer", "enabled", "route_with_shadow",
           "record_outcome", "read_rows", "summarize", "summarize_jev", "main",
           "qc_with_shadow"]

#: Master switch: the shadow hook runs only when this reads ``1/true/yes/on``.
ENV_FLAG = "PLANGRAPH_PLANNER_SHADOW"
#: Default audit-log path when ``log_path`` is not passed.
ENV_LOG = "PLANGRAPH_PLANNER_SHADOW_LOG"

#: Values that turn the hook on (case-insensitive); anything else is off.
_ON = frozenset({"1", "true", "yes", "on"})

#: Card fields that must never reach the log (redaction).
_SECRET_FIELDS = frozenset({"heldout_check", "check_cmd", "write_set"})

#: ``answerer(card, rules_route)`` returns a JSON-able mapping (e.g. the legs it would pick).
Answerer = Callable[[Mapping, Route], Mapping]

#: Sentinel that distinguishes "attempt not given" from "attempt given as None".
_UNSET = object()


def _env(env: Mapping | None) -> Mapping:
    return os.environ if env is None else env


def enabled(env: Mapping | None = None) -> bool:
    """True only for ``PLANGRAPH_PLANNER_SHADOW`` in ``1/true/yes/on``; default off."""
    raw = _env(env).get(ENV_FLAG, "")
    return str(raw).strip().lower() in _ON


def _log_path(log_path, env: Mapping | None) -> str | None:
    path = log_path if log_path is not None else _env(env).get(ENV_LOG)
    if path is None:
        return None
    text = str(path).strip()
    return text or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append(path: str, row: Mapping) -> bool:
    """Append one JSON line; log failures are swallowed, never raised."""
    try:
        line = json.dumps(row, default=repr)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, ValueError, TypeError):
        return False
    return True


def _scrub(value):
    """Drop the redacted card fields (recursively) from a shadow answer."""
    if isinstance(value, Mapping):
        return {k: _scrub(v) for k, v in value.items() if k not in _SECRET_FIELDS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _first_leg_id(answer) -> str | None:
    """First leg id of a shadow answer (list of ids, or of ``{"id": ...}`` maps)."""
    if not isinstance(answer, Mapping):
        return None
    legs = answer.get("legs")
    if not legs or isinstance(legs, (str, bytes)):
        return None
    first = legs[0]
    if isinstance(first, Mapping):
        first = first.get("id")
    return None if first is None else str(first)


def _jev_sink(log_path, env: Mapping | None, store, principal):
    """The Jev sink: the keeper store when given, else the JSONL log path, else None."""
    if store is not None and principal is not None:
        return jev_shadow.store_sink(store, principal)
    path = _log_path(log_path, env)
    if path is not None:
        return jev_shadow.jsonl_sink(path)
    return None


def _run_jev_hook(card, rules, *, log_path, env: Mapping | None, store, principal,
                  attempt, jev_cfg) -> None:
    """Run the Jev shadow runner beside the legacy hook; never raises.

    Independent of ``PLANGRAPH_PLANNER_SHADOW``: when Jev is off (``PLANGRAPH_JEV``
    absent or ``off``) :func:`jev_shadow.shadow_routing` is a cheap no-op.  The sink
    prefers the keeper store when ``store`` and ``principal`` are given, so a wired-in
    store is the single writer; otherwise the resolved JSONL log path is used, else
    there is no sink.  Any escape is swallowed (the runner itself already never raises).
    """
    try:
        cfg = jev_cfg or jev_shadow.jev_client.config_from_env(env)
        if not jev_shadow.jev_client.enabled(cfg):
            return
        sink = _jev_sink(log_path, env, store, principal)
        observed = copy.deepcopy(card)
        observed_rules = copy.deepcopy(rules)
        jev_shadow.background(lambda: jev_shadow.shadow_routing(
            observed, observed_rules, sink=sink, attempt=attempt, cfg=cfg, env=env))
    except Exception:  # noqa: BLE001 - belt and braces: the hook itself never raises
        pass


def qc_with_shadow(verdict, phase, key=None, *, log_path=None,
                   env: Mapping | None = None, store=None, principal=None,
                   cfg=None) -> list[dict]:
    """Shadow the ``plan_actions`` accept/reject decision; rows only, never raises.

    The QC twin of :func:`_run_jev_hook`, called from the plan's own call site: the
    sink prefers the keeper store when ``store`` and ``principal`` are given, else the
    resolved JSONL log path (``log_path`` or ``PLANGRAPH_PLANNER_SHADOW_LOG``), else
    there is no sink.  Independent of ``PLANGRAPH_PLANNER_SHADOW``: Jev's own switch
    (``PLANGRAPH_JEV``) decides, and a disabled mode is a cheap no-op.  Any escape is
    swallowed and returns ``[]``, so the plan can never change.
    """
    try:
        sink = _jev_sink(log_path, env, store, principal)
        return jev_shadow.qc_hook(sink=sink, cfg=cfg, env=env)(verdict, phase, key)
    except Exception:  # noqa: BLE001 - the hook itself never raises
        return []


def route_with_shadow(card: Mapping, *, answerer: Answerer | None = None,
                      log_path=None, env: Mapping | None = None, store=None,
                      principal=None, attempt=None, jev_cfg=None,
                      **route_kw) -> tuple[Route, str | None]:
    """Route ``card`` with the rules and audit a shadow answer beside it.

    The rules route from ``route_card`` (its exceptions propagate unchanged) is always
    computed first and always returned unmodified; the returned id names the legacy
    audit row (``None`` when that hook is off or no log path resolves).  The answerer
    sees a deep copy of the card and route, and its failure is recorded and ignored.

    Independently of the legacy hook the Jev shadow runner is called once (see
    :func:`_run_jev_hook`); it can never change ``rules`` or the returned id.  The card
    must carry its own ``key`` for a Jev row to be recorded.
    """
    rules = route_card(card, **route_kw)
    _run_jev_hook(card, rules, log_path=log_path, env=env, store=store,
                  principal=principal, attempt=attempt, jev_cfg=jev_cfg)
    if not enabled(env) or answerer is None:
        return rules, None
    path = _log_path(log_path, env)
    if path is None:
        return rules, None

    decision_id = uuid.uuid4().hex
    shadow = None
    shadow_error = None
    try:
        shadow = answerer(copy.deepcopy(card), copy.deepcopy(rules))
    except Exception as exc:  # a broken shadow must never break routing
        shadow_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    answer = _scrub(shadow) if shadow is not None else None
    first = _first_leg_id(answer)
    agree = None if first is None else first == rules.legs[0].id
    row = {"v": 1, "event": "decision", "id": decision_id, "ts": _now(),
           "card_key": card.get("key"),
           "rules": {"window_class": rules.window_class,
                     "legs": [leg.id for leg in rules.legs],
                     "review_seats": rules.review_seats,
                     "reasons": list(rules.reasons)},
           "shadow": answer, "shadow_error": shadow_error, "agree": agree}
    _append(path, row)
    return rules, decision_id


def _legacy_outcome(decision_id, outcome, log_path, env) -> bool:
    """The legacy ``outcome`` write; unchanged D18 semantics (False when it did not land)."""
    if decision_id is None or not enabled(env):
        return False
    path = _log_path(log_path, env)
    if path is None:
        return False
    row = {"v": 1, "event": "outcome", "id": decision_id, "ts": _now(),
           "outcome": dict(outcome)}
    return _append(path, row)


def _outcome_text(outcome) -> str:
    """The short string a keeper Jev outcome gets: ``exit_class``/``outcome``/recorded."""
    if isinstance(outcome, Mapping):
        value = outcome.get("exit_class") or outcome.get("outcome") or "recorded"
    else:
        value = "recorded"
    return str(value)[:200]


def record_outcome(decision_id, outcome: Mapping, *, log_path=None,
                   env: Mapping | None = None, store=None, principal=None,
                   card_key=None, attempt=_UNSET) -> bool:
    """Append the legacy attempt ``outcome`` and fill the Jev rows' outcome.

    The legacy JSONL write is unchanged (``False`` for a ``None`` id, the flag off or no
    log path).  Independently, when ``store``, ``principal`` and ``card_key`` are given
    the keeper Jev rows for that card are filled via
    :func:`plangraph.planner.jev_shadow.fill_outcome`; ``attempt`` is forwarded only
    when given (omitting it fills only unnumbered routing rows). That fill depends on neither the
    flags nor ``decision_id``.  True when either write landed.
    """
    legacy = _legacy_outcome(decision_id, outcome, log_path, env)
    filled = 0
    if store is not None and principal is not None and card_key is not None:
        text = _outcome_text(outcome)
        filled = sum(jev_shadow.fill_outcome(
            store, principal, card_key, text,
            attempt=None if attempt is _UNSET else attempt, kind=kind)
            for kind in jev_shadow.jev_questions.ROUTING_KINDS)
    return legacy or filled > 0


def read_rows(log_path) -> list[dict]:
    """Read the JSONL audit log, skipping blank, malformed or non-object lines."""
    rows: list[dict] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def summarize(rows) -> dict:
    """Counts over the audit rows, outcomes joined to decisions by id."""
    decisions = [r for r in rows if isinstance(r, dict) and r.get("event") == "decision"]
    ids = {r.get("id") for r in decisions}
    outcomes = sum(1 for r in rows if isinstance(r, dict)
                   and r.get("event") == "outcome" and r.get("id") in ids)
    agree = sum(1 for r in decisions if r.get("agree") is True)
    disagree = sum(1 for r in decisions if r.get("agree") is False)
    errors = sum(1 for r in decisions if r.get("shadow_error"))
    return {"decisions": len(decisions), "outcomes": outcomes, "agree": agree,
            "disagree": disagree, "shadow_errors": errors}


def summarize_jev(rows) -> dict:
    """The Jev agreement report over the JSONL ``jev`` rows.

    Keeps the rows the Jev runner wrote through :func:`jev_shadow.jsonl_sink`
    (``event == "jev"``) and returns
    :func:`plangraph.keeper.jev_report.agreement_report` of them - the same numbers the
    keeper's ``GET /v1/jev_report`` serves from stored rows.  Those JSONL rows carry
    ``kind``/``rule_answer``/``jev_answer``/``status`` but no ``outcome``, which simply
    counts as no outcome.
    """
    jev_rows = [row for row in rows
                if isinstance(row, Mapping) and row.get("event") == "jev"]
    return jev_report.agreement_report(jev_rows)


def _summary_line(summary: Mapping) -> str:
    """One line for a :func:`summarize` result."""
    return ("decisions %s  outcomes %s  agree %s  disagree %s  shadow_errors %s"
            % (summary.get("decisions"), summary.get("outcomes"),
               summary.get("agree"), summary.get("disagree"),
               summary.get("shadow_errors")))


def main(argv=None) -> int:
    """``python3 -m plangraph.planner.shadow report [--log PATH] [--json]``.

    Reads the audit log (``--log`` or ``PLANGRAPH_PLANNER_SHADOW_LOG``) and prints the
    legacy :func:`summarize` line plus the Jev agreement table from :func:`summarize_jev`
    and :func:`plangraph.keeper.jev_report.render`; ``--json`` prints both as one
    ``{"summary": ..., "jev": ...}`` object.  With no path it prints a hint to stderr
    and returns 2; an unknown subcommand is an argparse error.  The keeper-side report
    is ``GET /v1/jev_report``.
    """
    parser = argparse.ArgumentParser(prog="python3 -m plangraph.planner.shadow",
                                     description="Read the planner shadow audit log.")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="summarise the shadow log")
    report.add_argument("--log", default=None,
                        help="JSONL log path (default PLANGRAPH_PLANNER_SHADOW_LOG)")
    report.add_argument("--json", action="store_true",
                        help="print one JSON object instead of text")
    args = parser.parse_args(argv)
    path = _log_path(args.log, None)
    if path is None:
        print("no shadow log: pass --log or set PLANGRAPH_PLANNER_SHADOW_LOG",
              file=sys.stderr)
        return 2
    rows = read_rows(path)
    summary = summarize(rows)
    jev = summarize_jev(rows)
    if args.json:
        print(json.dumps({"summary": summary, "jev": jev}))
    else:
        print(_summary_line(summary))
        print(jev_report.render(jev))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
