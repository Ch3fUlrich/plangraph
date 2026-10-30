"""Route policy: which leg families and harnesses ``plan_spawn`` skips (OPERATOR D-3198).

Policy is data, not code: ``route_policy.json`` next to this module holds the
operator's current lists; ``PLANGRAPH_ROUTE_POLICY`` names a replacement file.

* ``banned_families`` -- never routed (kimi, deepseek);
* ``off_families`` -- switched off for now (glm);
* ``paused_harnesses`` -- ``{harness: {"allowed_roles": [...]}}``: a paused
  harness serves only the listed roles (qodercli: review seats only).

Skips are recorded in the plan with reason ``banned_family`` / ``off_family`` /
``paused_harness``.  Pure data and validation; the only I/O is reading the file.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["RoutePolicy", "load_route_policy", "DEFAULT_POLICY_PATH"]

DEFAULT_POLICY_PATH = Path(__file__).with_name("route_policy.json")
_ROLES = ("implement", "review", "orchestrate")
_KEYS = ("banned_families", "off_families", "paused_harnesses")


@dataclass(frozen=True)
class RoutePolicy:
    banned_families: tuple[str, ...] = ()
    off_families: tuple[str, ...] = ()
    paused_harnesses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def allows(self, harness: str, role: str) -> bool:
        """False when ``harness`` is paused and ``role`` is not one it still serves."""
        allowed = self.paused_harnesses.get(harness)
        return allowed is None or role in allowed

    def skip_reason(self, leg, role: str) -> str | None:
        # Keeper families too: "zhipu" bans glm legs, "deepseek" every deepseek spelling.
        from .seats import seat_family

        def listed(families):
            return bool({leg.family, seat_family(leg.family)}
                        & ({seat_family(f) for f in families} | set(families)))
        if listed(self.banned_families):
            return "banned_family"
        if listed(self.off_families):
            return "off_family"
        if not self.allows(leg.harness, role):
            return "paused_harness"
        from .legs import NO_VERDICT_SEATS
        if role == "review" and leg.model in NO_VERDICT_SEATS:
            return "no_verdict_seat"
        return None


def _str_list(doc, key, problems):
    value = doc.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        problems.append(f"{key}: must be a list of nonempty strings")
        return ()
    return tuple(value)


def load_route_policy(path: Path | str | None = None) -> RoutePolicy:
    """Read the policy at ``path``, else ``$PLANGRAPH_ROUTE_POLICY``, else the default.

    Raises ``ValueError`` listing every problem found.
    """
    if path is None:
        path = os.environ.get("PLANGRAPH_ROUTE_POLICY") or DEFAULT_POLICY_PATH
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read route policy: {exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"route policy is not valid JSON: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise ValueError("route policy must be an object")
    problems: list[str] = []
    for key in doc:
        if key not in _KEYS and not key.startswith("_"):
            problems.append(f"unknown field {key!r}")
    banned = _str_list(doc, "banned_families", problems)
    off = _str_list(doc, "off_families", problems)
    paused: dict[str, tuple[str, ...]] = {}
    raw = doc.get("paused_harnesses", {})
    if not isinstance(raw, Mapping):
        problems.append("paused_harnesses: must be an object")
        raw = {}
    for harness, entry in raw.items():
        roles = entry.get("allowed_roles") if isinstance(entry, Mapping) else None
        if (not isinstance(roles, list) or any(r not in _ROLES for r in roles)
                or set(entry) - {"allowed_roles"}):
            problems.append(f"paused_harnesses.{harness}: needs allowed_roles, a subset of {_ROLES}")
            continue
        paused[harness] = tuple(roles)
    if problems:
        raise ValueError("invalid route policy: " + "; ".join(problems))
    return RoutePolicy(banned, off, paused)
