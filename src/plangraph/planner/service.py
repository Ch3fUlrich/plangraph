"""Glue between the planner modules and a keeper ``Store`` (design 4.2 - 4.4).

Everything here is portable: reads go through ``store.conn.execute`` with ``?``
placeholders (SQLite and Postgres) and writes use the store verbs ``put_card``,
``set_state`` and ``ask_question``.

* :func:`running_write_sets`: write sets of the cards whose item is ``running``.
* :func:`gate_card`: route the card, run the readiness gate and move the item to
  ``ready`` or ``needs_input`` (with one planner question listing the findings).
* :func:`route_for_card` / :func:`pack_for_card`: routing and context pack for a stored card.
* :func:`import_batch`: read the cards of a batch YAML and store them.

Note on ``risk_tags``: the keeper stores ``'[]'`` for both "absent" and "reviewed, none",
so a stored card always reads back with a list.  The readiness code ``no_risk_tags`` is
therefore reachable only through direct ``readiness.check_card`` calls; ``gate_card``
passes the stored value unchanged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Sequence

from . import card as card_mod
from . import context_pack, readiness, routing
from .legs import DEFAULT_LEGS, Leg

__all__ = ["GATEABLE_STATES", "GateResult", "running_write_sets", "gate_card",
           "route_for_card", "pack_for_card", "import_batch"]

#: Item states the gate may act on; any other state is left alone.
GATEABLE_STATES = ("open", "needs_input", "ready")
_QUESTION_CAP = 2000
_REASON_CAP = 500


@dataclass(frozen=True)
class GateResult:
    """Outcome of :func:`gate_card`.

    ``acted`` is False when the item state is not gateable (nothing was written).
    ``state`` is the item state after the call; ``question_id`` is the open question
    that carries the findings (new or already there), ``None`` when the card is ready.
    """

    key: str
    verdict: readiness.Verdict
    route: routing.Route | None
    route_error: str | None
    acted: bool
    state: str | None
    question_id: int | None

    @property
    def ready(self) -> bool:
        return self.verdict.ready


def _load_list(value: Any) -> list:
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or [])
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def running_write_sets(store: Any, project: str,
                       exclude: str | None = None) -> dict[str, list[str]]:
    """Write sets of the cards of ``project`` whose item state is ``running``."""
    rows = store.conn.execute(
        "SELECT c.key, c.write_set FROM cards c JOIN items i ON i.key = c.key"
        " WHERE i.project = ? AND i.state = 'running' ORDER BY c.key", (project,)).fetchall()
    return {row[0]: [str(p) for p in _load_list(row[1])] for row in rows
            if row[0] != exclude}


def _item_state(store: Any, key: str) -> str | None:
    row = store.conn.execute("SELECT state FROM items WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _question_text(verdict: readiness.Verdict) -> str:
    lines = [verdict.summary()]
    lines += [f"{f.code}: {f.message}" for f in verdict.findings]
    return "\n".join(lines)[:_QUESTION_CAP]


def _open_question(store: Any, key: str, text: str) -> int | None:
    row = store.conn.execute(
        "SELECT id FROM questions WHERE key = ? AND state = 'open' AND text = ?"
        " ORDER BY id LIMIT 1", (key, text)).fetchone()
    return None if row is None else row[0]


def gate_card(store: Any, principal: Any, key: str, *, repo_root: Path | str,
              legs: Sequence[Leg] = DEFAULT_LEGS,
              dry_run: Callable[[str, Path], tuple[bool, str]] | None = None,
              subtasks: int = 1, unavailable: Collection[str] = (),
              require_heldout: bool = False) -> GateResult:
    """Gate the stored card of ``key`` and move its item to ``ready`` or ``needs_input``.

    A route failure adds a ``no_route`` finding.  A context pack above the first leg's
    ``max_request_tokens`` adds ``leg_request_too_big`` (built best effort: a pack that
    cannot be built at all is skipped, a ``PackTooBig`` is reported).  Only items in
    ``GATEABLE_STATES`` are touched.  Not ready: ``needs_input`` plus one open question
    without default (the planner queue); an identical open question is reused, so a
    repeat call writes nothing.
    """
    card = store.get_card(principal, key)
    project = key.split(":", 1)[0]
    route: routing.Route | None = None
    route_error: str | None = None
    try:
        route = routing.route_card(card, legs=legs, subtasks=subtasks,
                                   unavailable=unavailable)
    except ValueError as exc:  # RouteError is a ValueError
        route_error = str(exc)
    leg_max = route.first_leg.max_request_tokens if route is not None else None
    pack_tokens: int | None = None
    pack_error: str | None = None
    if route is not None:
        try:
            pack_tokens = context_pack.build_pack(
                card, repo_root=Path(repo_root), key=key,
                budget_tokens=context_pack.pack_budget(route.window_class)).tokens
        except context_pack.PackTooBig as exc:
            pack_tokens = None
            pack_error = str(exc)
        except (ValueError, OSError):
            pack_tokens = None  # best effort: no pack, no pack finding
    kw: dict[str, Any] = {}
    if dry_run is not None:
        kw["dry_run"] = dry_run
    verdict = readiness.check_card(
        card, key=key, repo_root=Path(repo_root),
        running_write_sets=running_write_sets(store, project, exclude=key),
        leg_max_request=leg_max, pack_tokens=pack_tokens,
        require_heldout=require_heldout, **kw)
    if route_error is not None:
        verdict = readiness.Verdict(
            key, verdict.findings + (readiness.Finding("no_route", route_error),))
    if pack_error is not None:
        verdict = readiness.Verdict(
            key, verdict.findings + (
                readiness.Finding("leg_request_too_big", f"pack too big: {pack_error}"),))

    state = _item_state(store, key)
    if state not in GATEABLE_STATES:
        return GateResult(key, verdict, route, route_error, False, state, None)
    if verdict.ready:
        if state != "ready":
            store.set_state(principal, key, "ready", reason="readiness gate ok")
        return GateResult(key, verdict, route, route_error, True, "ready", None)
    store.set_state(principal, key, "needs_input", reason=verdict.summary()[:_REASON_CAP])
    text = _question_text(verdict)
    qid = _open_question(store, key, text)
    if qid is None:
        qid = store.ask_question(principal, key, text, default_answer=None,
                                 risk_tags=list(card.get("risk_tags") or []))["id"]
    return GateResult(key, verdict, route, route_error, True, "needs_input", qid)


def route_for_card(store: Any, principal: Any, key: str, **route_kw: Any) -> routing.Route:
    """Route the stored card of ``key`` (``route_kw`` goes to ``routing.route_card``)."""
    return routing.route_card(store.get_card(principal, key), **route_kw)


def pack_for_card(store: Any, principal: Any, key: str, *, repo_root: Path | str,
                  window_class: str | None = None,
                  check_tail: str | None = None) -> context_pack.Pack:
    """Build the context pack of the stored card of ``key``.

    The budget comes from ``window_class`` or, failing that, the card's own class;
    ``ValueError`` when neither is set.
    """
    card = store.get_card(principal, key)
    window = window_class or card.get("window_class")
    if window is None:
        raise ValueError("window_class is required: neither given nor set on the card")
    return context_pack.build_pack(
        card, repo_root=Path(repo_root), key=key,
        budget_tokens=context_pack.pack_budget(window), check_tail=check_tail)


def import_batch(store: Any, principal: Any, path: Path | str,
                 default_project: str | None = None) -> list[str]:
    """Store the cards of the batch YAML at ``path``; return the keys written.

    ``default_project`` qualifies bare card ids (otherwise the batch ``project`` does).
    """
    if default_project is None:
        cards = card_mod.load_cards_yaml(path)
    else:
        import yaml

        with open(path, encoding="utf-8") as handle:
            cards = card_mod.cards_from_batch(yaml.safe_load(handle), default_project)
    return card_mod.apply_cards(store, principal, cards)
