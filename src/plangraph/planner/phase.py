"""Planner half of the Phase runner (S1): plan a gate's cards for ``phase_plan``.

Pure planning: reads the phase gate by key, its card edges and the cards; routes
and gates every card; reports intra-plan write-set overlaps and startability.
It never writes to the store.  The only possible write is the shadow audit row,
which :func:`plangraph.planner.shadow.route_with_shadow` no-ops when the hook is
off.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

from plangraph.governor import policy
from plangraph.keeper.errors import Refused

from . import readiness, service, shadow, start
from .legs import DEFAULT_LEGS, Leg

__all__ = ["PHASE_LEGS", "plan_phase", "startable"]

#: P1 enables only the opencode harness.
PHASE_LEGS: tuple[Leg, ...] = tuple(
    leg for leg in DEFAULT_LEGS if leg.harness == "opencode")

#: States in which a card may be planned / gated.
_GATEABLE_STATES = ("open", "needs_input", "ready")
#: States that make a card "busy" (not planned, reported in ``busy``).
_BUSY_STATES = ("running", "parked", "in_review", "claimed")
#: States that close the phase gate itself.
_TERMINAL_STATES = ("done", "cancelled")

def _as_paths(value: Any) -> list[str]:
    """Normalise a stored write/read set (list, tuple or JSON string) to strings."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(p) for p in value]

def _write_set(card: Mapping) -> list[str]:
    return _as_paths(card.get("write_set"))

def _route_dict(route: Any) -> dict | None:
    """The JSON view of a :class:`routing.Route` (``None`` for a failed route)."""
    if route is None:
        return None
    return {
        "window_class": route.window_class,
        "harness": route.harness,
        "legs": [leg.id for leg in route.legs],
        "review_seats": route.review_seats,
    }

def plan_phase(store: Any, principal: Any, key: str, *, repo_root: Path | str,
               now: datetime | None = None,
               legs: Sequence[Leg] = PHASE_LEGS,
               source: policy.Source | None = None,
               answerer: shadow.Answerer | None = None,
               unavailable: Collection[str] = (),
               initiator: str = "agent") -> dict:
    """Plan the cards of the phase gate ``key`` (writes nothing to the store)."""
    # Role first, before reading anything at all.
    role = getattr(principal, "role", None)
    if role not in ("router", "system"):
        raise Refused("forbidden", "phase_plan requires router or system")

    row = store.conn.execute(
        "SELECT kind, state FROM items WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise Refused("not_found", f"no item {key!r}")
    kind, state = row[0], row[1]
    if kind != "gate":
        raise Refused("validation", f"{key} is a {kind}, not a gate")
    if state in _TERMINAL_STATES:
        raise Refused("bad_state", f"{key} is {state}")

    project = key.split(":", 1)[0]
    if now is None:
        now = datetime.now(timezone.utc)
    source = source or policy.StaticSource()

    rows = store.conn.execute(
        "SELECT dst FROM edges WHERE src = ? ORDER BY dst", (key,)).fetchall()
    dsts = sorted({r[0] for r in rows})

    cards: list[dict] = []
    skipped: list[dict] = []
    busy: list[dict] = []

    for dst in dsts:
        card_row = store.conn.execute(
            "SELECT 1 FROM cards WHERE key = ?", (dst,)).fetchone()
        if card_row is None:
            skipped.append({"key": dst, "why": "missing_card"})
            continue

        item = store.conn.execute(
            "SELECT state FROM items WHERE key = ?", (dst,)).fetchone()
        card_state = item[0] if item is not None else None
        if card_state == "done":
            skipped.append({"key": dst, "why": "already_done"})
            continue
        if card_state in _BUSY_STATES:
            busy.append({"key": dst, "state": card_state})
            continue
        if card_state not in _GATEABLE_STATES:
            continue

        card = store.get_card(principal, dst)

        # Route first so the leg's request cap can feed the readiness gate.
        route = None
        route_error = None
        shadow_id = None
        try:
            route, shadow_id = shadow.route_with_shadow(
                card, answerer=answerer, legs=legs, unavailable=unavailable)
        except ValueError as exc:  # RouteError is a ValueError
            route = None
            shadow_id = None
            route_error = str(exc)

        # The governor is called per planned card, with the routed leg family.
        start_allowed = False
        start_reason = None
        if route is not None:
            gate = start.guard_start(
                "worker_leg", initiator, now,
                source=source, legs=legs,
                provider=route.first_leg.family)
            start_allowed = bool(gate.allowed)
            start_reason = gate.decision.reason

        leg_max = route.first_leg.max_request_tokens if route is not None else None
        running = service.running_write_sets(store, project, exclude=dst)
        verdict = readiness.check_card(
            card, key=dst, repo_root=Path(repo_root),
            running_write_sets=running, leg_max_request=leg_max)

        cards.append({
            "key": dst,
            "state": card_state,
            "write_set": _write_set(card),
            "ready": bool(verdict.ready),
            "findings": [{"code": f.code, "message": f.message}
                         for f in verdict.findings],
            "start_allowed": start_allowed,
            "start_reason": start_reason,
            "route": _route_dict(route),
            "route_error": route_error,
            "shadow_id": shadow_id,
            "deferred": False,
        })

    # Intra-plan write-set overlaps (cards are in key order).
    overlaps: list[dict] = []
    deferred: set[str] = set()
    for i in range(len(cards)):
        a = cards[i]
        for j in range(i + 1, len(cards)):
            b = cards[j]
            hits = readiness.write_sets_disjoint(
                a["write_set"], {b["key"]: b["write_set"]})
            for path_a, _other_key, path_b in hits:
                overlaps.append({"a": a["key"], "b": b["key"],
                                 "path_a": path_a, "path_b": path_b})
            if hits:
                deferred.add(b["key"])
    for c in cards:
        if c["key"] in deferred:
            c["deferred"] = True

    blocked: list[str] = []
    if not legs:
        blocked.append("no opencode leg")

    startable_cards = [c for c in cards
                       if c["ready"] and c["route"] is not None
                       and not c["deferred"] and c["start_allowed"]]
    ready = bool(not blocked and startable_cards)

    return {
        "phase": key,
        "ready": ready,
        "blocked": blocked,
        "cards": cards,
        "overlaps": overlaps,
        "skipped": skipped,
        "busy": busy,
    }

def startable(plan: Mapping) -> list[str]:
    """Keys of the cards that may start now (ready, routed, not deferred, allowed)."""
    return [c["key"] for c in plan["cards"]
            if c["ready"] and c["route"] is not None
            and not c["deferred"] and c["start_allowed"]]
