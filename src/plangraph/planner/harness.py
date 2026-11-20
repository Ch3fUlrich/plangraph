"""Own-harness-first routing (rule 3.53, D-6310; operator 2026-11-20).

A model runs in the harness it is trained on: gpt in Codex, gemini in agy, claude in
Claude Code or agy.  OpenCode serves such a model only when its client is not installed
on the placed host, and the leg then says why (``harness_fallback``).  Families without
an own client (deepseek, glm, mimo, free legs) stay on OpenCode.  Pure data and
functions: the only I/O is the default client probe (``shutil.which``).
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from .legs import Leg

__all__ = ["OWN_HARNESS", "CLIENT_MISSING", "HarnessChoice", "client_installed", "resolve_harness",
           "own_harness_legs", "refuse_foreign_pin", "own_harness_model", "own_harness_enabled", "service_probe"]

#: Model family -> the harnesses that are its own; the first is the one the route starts.
OWN_HARNESS: dict[str, tuple[str, ...]] = {
    "gpt": ("codex",), "gemini": ("agy",), "claude": ("claude", "agy")}
CLIENT_MISSING = "client_missing"
#: ``PLANGRAPH_OWN_HARNESS=off`` turns own-harness routing off (legs keep their registered harness).
DISABLE_ENV = "PLANGRAPH_OWN_HARNESS"
#: Shortest override reason a foreign-harness pin accepts (same bar as the paid pin).
MIN_REASON = 10


@dataclass(frozen=True)
class HarnessChoice:
    harness: str
    fallback: str | None = None


def own_harness_enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() != "off"


def client_installed(name: str) -> bool:
    return shutil.which(name) is not None


def resolve_harness(leg: Leg, installed: Callable[[str], bool] | None = client_installed) -> HarnessChoice:
    """The harness a leg runs in: its family's own client when installed, else the leg's own
    (OpenCode) harness with ``client_missing``.  ``installed=None`` switches own-harness routing off."""
    harness = getattr(leg, "harness", None)
    own = OWN_HARNESS.get(getattr(leg, "family", None))
    # An inline-only leg is packaged for its OpenCode child; its model name is not the client's.
    if installed is None or not own or harness in own or getattr(leg, "tool_mode", "tools") != "tools":
        return HarnessChoice(harness)
    if installed(own[0]):
        return HarnessChoice(own[0])
    return HarnessChoice(harness, CLIENT_MISSING)


def service_probe(service) -> Callable[[str], bool] | None:
    """The client probe of a spawn service: its own ``client_probe``, else the host's PATH, or None
    (own-harness routing off) while ``PLANGRAPH_OWN_HARNESS=off``."""
    probe = getattr(service, "client_probe", None)
    if probe is not None:
        return probe
    return client_installed if own_harness_enabled() else None


def own_harness_model(leg: Leg, harness: str) -> str:
    """The model name the own client takes: OmniRoute's ``omniroute/cx-`` alias is the plain id."""
    if harness == "codex":
        name = leg.model.removeprefix("omniroute/")
        return name.removeprefix("cx-")
    return leg.model


def own_harness_legs(legs: Iterable[Leg], installed: Callable[[str], bool] | None = client_installed) -> tuple[Leg, ...]:
    """Each leg moved into its own harness, or marked with the fallback reason."""
    result = []
    for leg in legs:
        choice = resolve_harness(leg, installed)
        if choice.harness != getattr(leg, "harness", None):
            leg = replace(leg, harness=choice.harness, model=own_harness_model(leg, choice.harness))
        if choice.fallback:
            leg = replace(leg, harness_fallback=choice.fallback)
        result.append(leg)
    return tuple(result)


def refuse_foreign_pin(leg: Leg, override_reason, installed: Callable[[str], bool] | None = client_installed) -> None:
    """A pin to a model's OpenCode leg needs an override reason while its own client is installed."""
    from plangraph.routing_support.leg_state_flag import SpawnRefused
    choice = resolve_harness(leg, installed)
    if choice.fallback is not None or choice.harness == leg.harness:
        return
    if not isinstance(override_reason, str) or len(override_reason.strip()) < MIN_REASON:
        raise SpawnRefused(
            f"foreign-harness pin refused: {leg.id} runs in {leg.harness} but {leg.family} runs in "
            f"{OWN_HARNESS[leg.family][0]}; --override-reason required (rule 3.53)")
