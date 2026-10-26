"""Cross-family review leg selection (stdlib only).

The only family ban is reviewer family == writer family (D-1533): any
model family may review, and Claude is one leg among several.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "FAMILIES",
    "HIGH_RISK",
    "is_high_risk",
    "Leg",
    "LEGS",
    "FamilyError",
    "NoEligibleLeg",
    "FleetFull",
    "model_family",
    "leg_for",
    "pick_legs",
    "families_for_risk",
    "check_cross_family",
    "writer_key",
]

FAMILIES = (
    (r"gpt|^o\d", "openai"), (r"mimo", "xiaomi"), (r"llama|muse", "meta"),
    (r"deepseek", "deepseek"), (r"qwen", "alibaba"), (r"gemini|gemma", "google"),
    (r"claude|sonnet|opus|haiku", "anthropic"),
    (r"glm|zhipu", "zhipu"),
    (r"nemotron", "nvidia"),
    (r"inkling", "thinkingmachines"),
    (r"north|cohere", "cohere"),
)

# Risk tags that need two reviewer families at phase end.
HIGH_RISK = frozenset({"auth", "secrets", "live-step", "public-release", "high"})
# A normalised tag containing any of these is high risk ("High-Risk", "live_step", "money-transfer").
_HIGH_RISK_PARTS = ("auth", "secret", "money", "live", "public-release", "high", "critical")


def _risk_tag(value: object) -> str:
    """Normalised risk tag: trimmed, lowercase, ``_``/space become ``-``."""
    return re.sub(r"[_\s]+", "-", str(value or "").strip().lower())


def is_high_risk(tags: object) -> bool:
    """True when any tag (one or several) is high risk after normalisation."""
    items = [tags] if tags is None or isinstance(tags, str) else list(tags)
    return any(part in _risk_tag(t) for t in items for part in _HIGH_RISK_PARTS)


class FamilyError(ValueError):
    """Reviewer family unusable (unknown, ambiguous, or writer's)."""


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


# Order is preference order. No leg is required: pick_legs skips the
# writer's family and any leg marked down.
LEGS = (
    Leg("openai", "gateway", "ovh/gpt-oss-120b", "openai"),
    Leg("qwen", "qoder", "Qwen3.8-Flash", "alibaba"),
    Leg("anthropic", "gateway", "claude-sonnet-4-5", "anthropic"),
    Leg("opencode", "opencode", "opencode/deepseek-v3", "deepseek"),
)


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


def _family_key(value: object) -> str:
    """Normalised family for a family name or model id (provider prefix, case, aliases).

    A known family name ("Anthropic") maps to itself; anything else goes through
    model_family ("anthropic/claude-x", "OVH/GPT-OSS", "deepseek"). An unrecognised
    string falls back to its lowercased form: fine for comparing a leg's own family,
    never for the WRITER (see `writer_key`).
    """
    low = _norm(value)
    if low in {family for _, family in FAMILIES}:
        return low
    return model_family(low) or low


def writer_key(writer_family: object) -> str:
    """Known family of the WRITER, or FamilyError: the one shared writer check.

    pick_legs, families_for_risk and check_cross_family all go through it, so
    they agree on an unknown writer string (empty, misspelt, ambiguous): fail
    closed. An unresolvable writer cannot be excluded from the reviewers, and
    "any family allowed" would let a writer review its own work.
    """
    key = _family_key(writer_family)
    if key not in {family for _, family in FAMILIES}:
        raise FamilyError(
            f"unknown writer family {writer_family!r}: cannot guarantee a cross-family review")
    return key


def pick_legs(writer_family: str, *, need: int = 1,
              down: frozenset[str] = frozenset(), fleet_busy: int = 0,
              fleet_cap: int = 4, legs: tuple[Leg, ...] | None = None,
              ) -> list[Leg]:
    """Pick up to `need` legs of distinct families unlike the writer's."""
    if fleet_busy + need > fleet_cap:
        raise FleetFull(
            f"fleet full: busy={fleet_busy} need={need} cap={fleet_cap}")
    writer = writer_key(writer_family)
    offline = {_norm(item) for item in (down or frozenset())}
    seen: set[str] = set()
    eligible: list[Leg] = []
    for leg in (LEGS if legs is None else legs):
        fam = _family_key(leg.family)
        if fam == writer or _norm(leg.name) in offline:
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


def families_for_risk(risk: str | list | tuple | set | frozenset | None,
                      writer_family: str,
                      legs: tuple[Leg, ...] | None = None) -> list[str]:
    """Reviewer families for a risk: 1 (standard) or 2 (high-risk).

    `risk` is one tag or several; any tag in HIGH_RISK needs two distinct
    families, both unlike the writer's. Raises NoEligibleLeg when the legs
    cannot supply that many, FamilyError for an unknown writer family.
    """
    if risk is None or isinstance(risk, str):
        tags = {_norm(risk)}
    else:
        tags = {_norm(item) for item in risk}
    need = 2 if (tags & HIGH_RISK or is_high_risk(tags)) else 1
    writer = writer_key(writer_family)
    found: list[str] = []
    for leg in (LEGS if legs is None else legs):
        fam = _family_key(leg.family)
        if fam != writer and fam not in found:
            found.append(fam)
    if len(found) < need:
        raise NoEligibleLeg(
            f"risk {sorted(tags)} needs {need} reviewer famil"
            f"{'y' if need == 1 else 'ies'} unlike writer {writer_family!r}; "
            f"only {len(found)} available")
    return found[:need]


def check_cross_family(writer_family: str, reviewer_model: str) -> str:
    """Reviewer family for a model id, refusing same/unknown."""
    if isinstance(reviewer_model, str) and _is_gateway_qwen(reviewer_model):
        raise FamilyError(
            f"model {reviewer_model!r} is not eligible (qwen is qoder-only)")
    family = model_family(reviewer_model)
    if family is None:
        raise FamilyError(
            f"unknown or ambiguous family for model {reviewer_model!r}")
    if _norm(family) == writer_key(writer_family):
        raise FamilyError(
            f"family {family!r} is the writer family {writer_family!r}")
    return family
