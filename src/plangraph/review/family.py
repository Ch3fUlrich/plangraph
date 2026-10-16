"""Cross-family review leg selection (stdlib only)."""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "FAMILIES",
    "Leg",
    "LEGS",
    "NEVER",
    "FamilyError",
    "NoEligibleLeg",
    "FleetFull",
    "model_family",
    "leg_for",
    "pick_legs",
    "check_cross_family",
]

FAMILIES = (
    (r"gpt|^o\d", "openai"), (r"mimo", "xiaomi"), (r"llama|muse", "meta"),
    (r"deepseek", "deepseek"), (r"qwen", "alibaba"), (r"gemini", "google"),
    (r"claude|sonnet|opus|haiku", "anthropic"),
)


class FamilyError(ValueError):
    """Reviewer family unusable (unknown, ambiguous, barred, or writer's)."""


class NoEligibleLeg(ValueError):
    """No review leg left for the writer family and down set."""


class FleetFull(RuntimeError):
    """Review fleet has no free seat for the requested legs."""


@dataclass(frozen=True)
class Leg:
    """One sanctioned tool-less review leg."""

    name: str
    kind: str
    model: str
    family: str


LEGS = (
    Leg("openai", "gateway", "ovh/gpt-oss-120b", "openai"),
    Leg("qwen", "qoder", "Qwen3.8-Flash", "alibaba"),
)

NEVER = ("anthropic",)


def model_family(model: str | None) -> str | None:
    """Family for a model id, or None when unknown or ambiguous."""
    name = str(model or "").lower().rsplit("/", 1)[-1]
    found = {family for pattern, family in FAMILIES if re.search(pattern, name)}
    return found.pop() if len(found) == 1 else None


def _is_gateway_qwen(model: str) -> bool:
    """True when the id routes qwen through the gateway (times out)."""
    low = str(model or "").lower()
    return "ovh/" in low and "qwen" in low


def leg_for(model: str | None) -> Leg | None:
    """Sanctioned leg serving a model id, or None when there is none."""
    if not isinstance(model, str) or not model:
        return None
    if _is_gateway_qwen(model):
        return None
    family = model_family(model)
    if family is None:
        return None
    for leg in LEGS:
        if leg.family == family:
            return leg
    return None


def _norm(value: object) -> str:
    """Lowercased stripped string for family/name comparison."""
    return str(value or "").strip().lower()


def pick_legs(writer_family: str, *, need: int = 1,
              down: frozenset[str] = frozenset(), fleet_busy: int = 0,
              fleet_cap: int = 4) -> list[Leg]:
    """Pick up to `need` legs of distinct families unlike the writer's."""
    if fleet_busy + need > fleet_cap:
        raise FleetFull(
            f"fleet full: busy={fleet_busy} need={need} cap={fleet_cap}")
    writer = _norm(writer_family)
    barred = {_norm(item) for item in NEVER}
    offline = {_norm(item) for item in (down or frozenset())}
    seen: set[str] = set()
    eligible: list[Leg] = []
    for leg in LEGS:
        fam = _norm(leg.family)
        if fam == writer or fam in barred or _norm(leg.name) in offline:
            continue
        if fam in seen:
            continue
        seen.add(fam)
        eligible.append(leg)
    if len(eligible) < need:
        raise NoEligibleLeg(
            f"no eligible review leg for writer family {writer_family!r} "
            f"with down={sorted(offline)} need={need}")
    return eligible[:need]


def check_cross_family(writer_family: str, reviewer_model: str) -> str:
    """Reviewer family for a model id, refusing same/barred/unknown."""
    if isinstance(reviewer_model, str) and _is_gateway_qwen(reviewer_model):
        raise FamilyError(
            f"model {reviewer_model!r} is not eligible (qwen is qoder-only)")
    family = model_family(reviewer_model)
    if family is None:
        raise FamilyError(
            f"unknown or ambiguous family for model {reviewer_model!r}")
    if _norm(family) in {_norm(item) for item in NEVER}:
        raise FamilyError(f"family {family!r} never reviews")
    if _norm(family) == _norm(writer_family):
        raise FamilyError(
            f"family {family!r} is the writer family {writer_family!r}")
    return family
