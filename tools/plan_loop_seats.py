"""Strict plan policy loading and pure seat selection; no spawning or retries."""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from plangraph.planner.legs import DEFAULT_LEGS, Leg
from plangraph.planner.seats import seat_family

LENSES = ("correctness and state", "security and operations", "simplicity and cost")
LADDER_KINDS = {"discovery", "stacked", "scope-cut", "escalated"}


class SelectionRefused(ValueError):
    """A selection cannot meet the requested seat count or ladder."""


@dataclass
class State:
    """Family names are oldest-use first; selection itself never mutates state."""

    level: int = 1
    used_families: list[str] = field(default_factory=list)
    lens_yields: dict[str, int] = field(default_factory=dict)
    tier: str = "standard"
    slug: str = "plan"
    previous_productive: bool = False

    def use(self, family: str) -> None:
        family = seat_family(family)
        self.used_families[:] = [f for f in self.used_families
                                 if seat_family(f) != family] + [family]

    def lens(self) -> str:
        return min(LENSES, key=lambda lens: self.lens_yields.get(lens, 0))

    def end_round(self, new_blocker_or_major: bool, *, counted: bool = True) -> None:
        if counted:
            if self.previous_productive and new_blocker_or_major:
                self.escalate()
            self.previous_productive = new_blocker_or_major

    def escalate(self) -> None:
        self.level += 1


@dataclass(frozen=True)
class SeatLeg:
    id: str
    rung: int
    training: bool


@dataclass(frozen=True)
class Policy:
    planner_family: str
    material: str
    accept_families: int
    max_rounds: int
    size_growth: float
    minor_max_chars: int
    max_file_chars: int
    budget_fraction: float
    chars_per_token: int
    seat_max_min: int
    first_content_min: int
    max_failed_attempts: int
    site_words_file: str
    fact_check_leg: tuple[str, ...]
    leg: tuple[SeatLeg, ...]


def load_policy(path: str | Path) -> Policy:
    """Refuse unknown fields and unsafe declarations against installed legs."""
    with Path(path).open("rb") as source:
        data = tomllib.load(source)
    if set(data) != set(Policy.__dataclass_fields__):
        raise ValueError("policy fields are unknown or missing")
    for key in ("planner_family", "material", "site_words_file"):
        if not isinstance(data[key], str):
            raise ValueError(f"invalid {key}")
    if not data["planner_family"] or data["material"] not in {"private", "scrubbed", "public"}:
        raise ValueError("invalid planner_family or material")
    for key in ("accept_families", "max_rounds", "minor_max_chars", "max_file_chars",
                "chars_per_token", "seat_max_min", "first_content_min", "max_failed_attempts"):
        if type(data[key]) is not int or data[key] <= 0:
            raise ValueError(f"invalid {key}")
    for key in ("size_growth", "budget_fraction"):
        value = data[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid {key}")
    if data["size_growth"] < 1 or data["budget_fraction"] > 1:
        raise ValueError("invalid growth or budget fraction")
    registry = {leg.id: leg for leg in DEFAULT_LEGS}
    configured = []
    if not isinstance(data["leg"], list) or not data["leg"]:
        raise ValueError("leg must be a nonempty list")
    for row in data["leg"]:
        if not isinstance(row, dict) or set(row) != {"id", "rung", "training"}:
            raise ValueError("unknown or missing leg field")
        if not isinstance(row["id"], str) or row["id"] not in registry:
            raise ValueError("unknown leg id")
        if any(item.id == row["id"] for item in configured):
            raise ValueError("duplicate leg id")
        leg = registry[row["id"]]
        if leg.claude_family or seat_family(leg.family) == seat_family(data["planner_family"]):
            raise ValueError("planner or claude family leg refused")
        if type(row["rung"]) is not int or row["rung"] < 1:
            raise ValueError("invalid rung")
        if type(row["training"]) is not bool:
            raise ValueError("invalid training flag")
        if not row["training"] and (leg.trains_on_data or leg.model.endswith("-contributor")):
            raise ValueError("training flag understates registry safety")
        configured.append(SeatLeg(**row))
    ids = {row.id for row in configured}
    facts = data["fact_check_leg"]
    if (not isinstance(facts, list) or not facts
            or any(not isinstance(id, str) or id not in ids for id in facts)
            or len(set(facts)) != len(facts)):
        raise ValueError("invalid fact_check_leg")
    return Policy(**{**data, "leg": tuple(configured), "fact_check_leg": tuple(facts)})


def pick(kind: str, state: State, policy: Policy) -> list[Leg]:
    """Return one seat (two distinct families for high discovery).

    Scrubbed training candidates remain eligible here: the bundle layer must
    verify the scrub record for the exact bytes before passing any material.
    R0 follows its explicit fact-check order. Other seats prefer unused families,
    then least recently used, with policy order breaking ties. No launch occurs.
    """
    if kind not in LADDER_KINDS | {"r0", "confirm", "diff"}:
        raise ValueError("unknown seat kind")
    if state.tier not in {"standard", "high"} or state.level < 1:
        raise ValueError("invalid selection state")
    registry = {leg.id: leg for leg in DEFAULT_LEGS}
    candidates = []
    for row in policy.leg:
        leg = registry[row.id]
        if leg.claude_family or seat_family(leg.family) == seat_family(policy.planner_family):
            continue
        if row.training and policy.material == "private":
            continue
        if kind in LADDER_KINDS and row.rung < state.level:
            continue
        if kind == "r0" and row.id not in policy.fact_check_leg:
            continue
        candidates.append(leg)
    if not candidates and kind in LADDER_KINDS:
        raise SelectionRefused(f"BLOCKED plan:{state.slug} | ladder-exhausted | "
                               "options: add a leg, *replan")
    recency = {seat_family(f): i for i, f in enumerate(state.used_families)}
    if kind == "r0":
        candidates.sort(key=lambda leg: policy.fact_check_leg.index(leg.id))
    else:
        candidates.sort(key=lambda leg: recency.get(seat_family(leg.family), -1))
    need = 2 if kind == "discovery" and state.tier == "high" else 1
    picked, families = [], set()
    for leg in candidates:
        family = seat_family(leg.family)
        if family not in families:
            picked.append(leg)
            families.add(family)
        if len(picked) == need:
            return picked
    raise SelectionRefused(f"BLOCKED plan:{state.slug} | review-loop-stalled | "
                           "options: wait for a family, *waive by D-row")
