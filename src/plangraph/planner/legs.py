"""Routing legs: the ordered model/harness combos the keeper may hand a card to (D25).

A *leg* is one concrete way to run a card: a harness, a model, the window classes it
serves and the largest request it accepts.  ``DEFAULT_LEGS`` lists them in the D25
preference order (position = preference).  The numbers are defaults; the measured
windows kept in the registry override them via :func:`with_measured`.  An operator
file (``{"legs": [...]}``) replaces the whole list via :func:`load_legs`.

Tiers run cheapest first: ``trial``, ``free``, ``subscription``, ``paid``.  The trial
tier is empty by default.  Pure data and validation; the only I/O is reading the
optional JSON override file.
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from plangraph.keeper.schema import WINDOW_CLASSES
from plangraph.mimo_policy import MimoPathError, check_mimo_path

__all__ = ["TIERS", "HARNESSES", "TOOL_MODES", "Leg", "DEFAULT_LEGS", "WRITER_ORDER", "leg_active",
           "load_legs", "with_measured", "EXPLICIT_LEGS", "EXPLICIT_WRITERS", "spawnable_legs",
           "CX_EFFORTS"]

#: Cost tiers, cheapest first.
TIERS = ("trial", "free", "subscription", "paid")
#: Harness names a leg may run under.
HARNESSES = ("qodercli", "opencode", "agy", "claude")
#: How a review leg reads the change: with its own tools, or only from a diff the
#: spawner inlines into the prompt (D-3134/D-3173: luna, mimo-pro give no verdict
#: in tool mode).
TOOL_MODES = ("tools", "inline-only", "prompt-only")
#: Models that never sit as a review seat, routed or hand-picked (D-3225:
#: opencode-go/qwen3.8-flash via OmniRoute returned empty content 6/6, ~289 s).
NO_VERDICT_SEATS = frozenset({"opencode-go/qwen3.8-flash"})
# Advisory only: these models remain routable, but explicit reviews warn.
REVIEW_UNFIT_SEATS = frozenset({"mimo/mimo-v2.6-flash"})
#: Reasoning-effort suffixes OmniRoute's cx provider serves (/v1/models, 2026-11-09).
CX_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Leg:
    """One routing leg (see module docstring)."""

    id: str
    tier: str
    harness: str
    model: str
    family: str
    windows: tuple[str, ...]
    max_request_tokens: int
    trains_on_data: bool
    claude_family: bool = False
    note: str = ""
    roles: tuple[str, ...] = ("orchestrate",)
    tool_mode: str = "tools"
    disabled: str | None = None


def _leg(id, tier, harness, model, family, windows, max_req, trains, claude=False,
         note=""):
    return Leg(id, tier, harness, model, family, tuple(windows), max_req, trains,
               claude, note)


#: D25 order.  Position is the preference inside a tier.
DEFAULT_LEGS: tuple[Leg, ...] = (
    _leg("qodercli-qwen3.8-flash", "free", "qodercli", "Qwen3.8-Flash", "qwen",
         ("128k", "256k"), 100000, False,
         note="free while qoder credits remain, until its rate limit"),
    _leg("mimo-v2.6-flash", "subscription", "opencode",
         "mimo/mimo-v2.6-flash", "mimo", ("128k", "256k", "1m"), 200000, False),
    _leg("agy-gemini-3.8-flash", "subscription", "agy", "gemini-3.8-flash", "gemini",
         ("128k", "256k", "1m"), 900000, False,
         note="gemini after its Friday-evening reset"),
    _leg("agy-sonnet-5.5", "subscription", "agy", "claude-sonnet-5-5-medium", "claude",
         ("128k", "256k", "1m"), 180000, False, claude=True,
         note="Antigravity quota, not Claude Max"),
    _leg("deepseek", "paid", "opencode", "omniroute/deepseek", "deepseek",
         ("128k", "256k"), 100000, False, note="pay-as-you-go, only if really needed"),
)

# D-2148/D-1704/D-2569: role orders are policy, not resolver fallbacks.
from dataclasses import replace as _replace
_legacy = {leg.id: leg for leg in DEFAULT_LEGS}
DEFAULT_LEGS = (
    _replace(_legacy["qodercli-qwen3.8-flash"], roles=("review",),
             disabled="D-4744 operator disabled, not a route option"),
    Leg("opencode-deepseek-v4.1-flash-free", "free", "opencode",
        "opencode/deepseek-v4.1-flash-free", "deepseek", ("128k",), 100000, True,
        roles=("review", "implement"),
        disabled="D-4196 opencode zen never worked, use opencode go; never remove (D-3828)"),
    Leg("opencode-glm-5.3-flash-free", "free", "opencode",
        "opencode/glm-5.3-flash-free", "glm", ("128k",), 100000, True,
        roles=("review", "implement"),
        disabled="D-4196 opencode zen never worked, use opencode go; never remove (D-3828)"),
    Leg("openrouter-free", "free", "opencode", "openrouter/free", "openrouter",
        ("128k",), 100000, True, roles=("implement",)),
    Leg("cx-gpt-6.1-sol", "subscription", "opencode", "omniroute/cx-gpt-6.1-sol-xhigh",
        "gpt", ("128k", "256k", "1m"), 900000, False, roles=("implement",),
        note="D-3605: operator implement leg (ChatGPT subscription via OmniRoute cx)"),
    Leg("cx-gpt-6-luna", "subscription", "opencode", "omniroute/cx-gpt-6-luna",
        "gpt", ("128k", "256k", "1m"), 900000, False, roles=("review",),
        tool_mode="inline-only"),
    _replace(_legacy["agy-gemini-3.8-flash"], roles=("implement", "review")),
    Leg("agy-gemini-4-argon", "subscription", "agy", "gemini-4-argon-high",
        "gemini", ("128k", "256k", "1m"), 900000, False, roles=("implement", "review")),
    Leg("opencode-big-pickle", "free", "opencode", "opencode/big-pickle",
        "glm", ("128k",), 100000, True, roles=("implement", "review")),
    Leg("agy-gpt-oss-120b", "subscription", "agy", "gpt-oss-120b-medium",
        "openai", ("128k",), 100000, False, roles=("review",)),
    _replace(_legacy["mimo-v2.6-flash"], roles=("implement", "review"), tool_mode="inline-only"),
    Leg("mimo-v2.6-pro", "subscription", "opencode", "mimo/mimo-v2.6-pro",
        "mimo", ("128k", "256k", "1m"), 200000, False,
        roles=("implement", "review", "orchestrate"), tool_mode="inline-only"),
    _replace(_legacy["agy-sonnet-5.5"], roles=("orchestrate", "review")),
    Leg("agy-claude-opus", "subscription", "agy", "claude-opus-4-6-thinking",
        "claude", ("128k", "256k"), 180000, False, claude_family=True, roles=("review",)),
    Leg("omniroute-deepseek-flash", "subscription", "opencode", "omniroute/deepseek-flash",
        "deepseek", ("128k",), 100000, False, roles=("review",)),
    Leg("omniroute-muse-spark", "subscription", "opencode", "omniroute/muse-spark-1.3-contributor",
        "muse", ("128k",), 100000, False, roles=("review",)),
    Leg("opencode-go-gemini-4-argon", "subscription", "opencode",
        "opencode-go/gemini-4-argon", "gemini", ("128k",), 100000,
        False, roles=("review",)),
    Leg("opencode-go-glm-5.2", "subscription", "opencode",
        "opencode-go/glm-5.2", "glm", ("128k",), 100000, False,
        roles=("review",), disabled="D-3605 operator: glm not acceptable"),
    Leg("opencode-go-qwen3.8-max", "subscription", "opencode",
        "opencode-go/qwen3.8-max", "qwen", ("128k",), 100000, False,
        roles=("review",), disabled="D-3605 operator: not acceptable"),
    Leg("omniroute-grok-4.7", "subscription", "opencode", "omniroute/gc/grok-4.7",
        "grok", ("128k", "256k"), 120000, False, roles=("review",), tool_mode="tools",
        note="D-3701: grok review seat via OmniRoute gc"),
    Leg("opencode-go-muse-spark", "subscription", "opencode",
        "opencode-go/muse-spark-1.3-contributor", "muse", ("128k",), 100000,
        False, roles=("review",)),
    Leg("opencode-console-muse-spark", "paid", "opencode",
        "console-meta/muse-spark-1.3-contributor", "muse", ("128k",), 100000,
        True, roles=("review", "implement"),
        note="D-5240: muse-amber, routed review seat and writer after subscription legs, "
             "pay-as-you-go; D-4280: scrubbed or public material only"),
    Leg("opencode-go-deepseek-v4-flash", "subscription", "opencode",
        "opencode-go/deepseek-v4.1-flash", "deepseek", ("128k",), 100000,
        False, roles=("review",)),
    Leg("omniroute-gemini-3.8-flash", "subscription", "opencode",
        "omniroute/gemini-3.8-flash", "gemini", ("128k",), 100000, True,
        roles=("review",),
        disabled="D-4193 upstream route not verified",
        note="D-4193 upstream route not verified, conservative"),
    _replace(_legacy["deepseek"], model="omniroute/deepseek-direct-flash", roles=("implement",)),
    # D-3605 Haiku leg: last, so it sorts after every non-claude review leg.
    Leg("claude-haiku-5.5", "subscription", "claude", "claude-haiku-5-5", "claude",
        ("128k", "256k"), 180000, False, claude_family=True, roles=("review",),
        tool_mode="prompt-only",
        note="D-3605/D-3619: review fallback via Claude subscription; never seats a claude/anthropic writer"),
)

#: D-5198: the console Muse review seat and fallback writer.
MUSE_AMBER = "opencode-console-muse-spark"
# D-5240 / rule 3.23a: tier, then table order; Muse writes only after subscriptions.
WRITER_ORDER: tuple[str, ...] = tuple(
    leg.id for leg in sorted(DEFAULT_LEGS, key=lambda leg: TIERS.index(leg.tier))
    if leg.id in ("cx-gpt-6.1-sol", "agy-gemini-3.8-flash", MUSE_AMBER))
# D-3828: retain every leg and its review order; only WRITER_ORDER legs write.
DEFAULT_LEGS = tuple(_replace(leg, roles=tuple(r for r in leg.roles if r != 'implement'),
                             disabled=leg.disabled or (
                                 'D-3828: writer disabled; no retained review or orchestrate role'
                                 if leg.roles == ('implement',) else None))
                     if leg.id not in WRITER_ORDER else leg for leg in DEFAULT_LEGS)
LEAD_WRITER = "cx-gpt-6.1-sol"

#: Operator 2026-11-09: Claude 5.5 seats and effort variants, spawnable by an
#: explicit ``--leg`` pick (or the model override) only.  They are NOT in
#: DEFAULT_LEGS, so no route, keeper plan or role order changes (D-3605: Claude is
#: no default writer or seat; D-3828: no writer fallback); ``SpawnService.route``
#: appends them as ``explicit_only`` skips, ``plan_spawn`` does not.  Model ids are what ``agy models`` and OmniRoute
#: /v1/models served that day (agy serves no Sonnet xhigh, cx no -ultra).  Claude
#: legs are claude-family, so the D-2511 fence keeps them off claude-written work.
#: A cx effort model needs a per-run opencode entry (``opencode_render``).
EXPLICIT_LEGS: tuple[Leg, ...] = (
    *(Leg(f"agy-claude-{m}-5-5-{e}", "subscription", "agy", f"claude-{m}-5-5-{e}",
          "claude", ("128k", "256k", "1m"), 900000, False, claude_family=True,
          roles=("review", "implement"), note="operator 2026-11-09: Antigravity quota")
      for m in ("sonnet", "opus") for e in ("low", "medium", "high")),
    *(Leg(f"agy-gemini-3.8-flash-{e}", "subscription", "agy", f"gemini-3.8-flash-{e}",
          "gemini", ("128k", "256k", "1m"), 900000, False, roles=("review",))
      for e in ("low", "medium", "high")),
    *(Leg(f"cx-gpt-6.1-sol-{e}", "subscription", "opencode", f"omniroute/cx-gpt-6.1-sol-{e}",
          "gpt", ("128k", "256k", "1m"), 900000, False, roles=("implement",))
      for e in CX_EFFORTS),
    *(Leg(f"cx-gpt-6-luna-{e}", "subscription", "opencode", f"omniroute/cx-gpt-6-luna-{e}",
          "gpt", ("128k", "256k", "1m"), 900000, False, roles=("review",),
          tool_mode="inline-only")
      for e in CX_EFFORTS),
)
#: Explicit legs the spawn service admits for ``implement`` besides LEAD_WRITER.
EXPLICIT_WRITERS: frozenset[str] = frozenset(
    leg.id for leg in EXPLICIT_LEGS if "implement" in leg.roles)


def spawnable_legs() -> tuple[Leg, ...]:
    """Every leg an explicit pick may name: the routed legs first, so a shared model
    resolves to its DEFAULT_LEGS leg.  Read at call time (callers and tests rebind
    DEFAULT_LEGS)."""
    return (*DEFAULT_LEGS, *EXPLICIT_LEGS)


def explicit_only_skips(role: str) -> tuple[dict, ...]:
    """`route` output rows for the explicit legs serving ``role``: listed, never routed."""
    return tuple({"leg": leg.id, "reason": "explicit_only", "evidence": {
        "kind": "explicit_only", "reason": "spawn with --leg " + leg.id}}
        for leg in EXPLICIT_LEGS if role in leg.roles)

_FIELDS = tuple(f.name for f in dataclasses.fields(Leg))
_REQUIRED = ("id", "tier", "harness", "model", "family", "windows",
             "max_request_tokens", "trains_on_data")
_STR_FIELDS = ("id", "tier", "harness", "model", "family", "note", "tool_mode")


def leg_active(leg: Leg) -> bool:
    """Local policy may disable a leg; OmniRoute owns temporal availability."""
    return leg.disabled is None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _build_leg(index: int, raw, problems: list[str]) -> Leg | None:
    where = f"legs[{index}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be an object")
        return None
    start = len(problems)
    if isinstance(raw.get("model"), str):
        try:
            check_mimo_path(raw["model"])
        except MimoPathError as exc:
            problems.append(f"{where}.model: {exc}")
    for key in raw:
        if key not in _FIELDS and key != "expires":
            problems.append(f"{where}: unknown field {key!r}")
    for key in _REQUIRED:
        if key not in raw:
            problems.append(f"{where}: missing field {key!r}")
    for key in _STR_FIELDS:
        if key in raw and not isinstance(raw[key], str):
            problems.append(f"{where}.{key}: must be a string")
    for key in ("id", "model", "family"):
        if isinstance(raw.get(key), str) and not raw[key]:
            problems.append(f"{where}.{key}: must not be empty")
    if isinstance(raw.get("tier"), str) and raw["tier"] not in TIERS:
        problems.append(f"{where}.tier: {raw['tier']!r} not in {TIERS}")
    if isinstance(raw.get("harness"), str) and raw["harness"] not in HARNESSES:
        problems.append(f"{where}.harness: {raw['harness']!r} not in {HARNESSES}")
    if isinstance(raw.get("tool_mode"), str) and raw["tool_mode"] not in TOOL_MODES:
        problems.append(f"{where}.tool_mode: {raw['tool_mode']!r} not in {TOOL_MODES}")
    windows = raw.get("windows")
    disabled = raw.get("disabled")
    if disabled is not None and (not isinstance(disabled, str) or not disabled.strip()):
        problems.append(f"{where}.disabled: must be a non-empty reason string")
    roles = raw.get("roles", ("orchestrate",))
    if (not isinstance(roles, (list, tuple))
            or (not roles and not (isinstance(disabled, str) and disabled.strip()))
            or any(not isinstance(r, str) or r not in ("implement", "review", "orchestrate") for r in roles)
            or len(set(roles)) != len(roles)):
        problems.append(f"{where}.roles: must be a subset of implement|review|orchestrate")
    if "windows" in raw:
        if (not isinstance(windows, (list, tuple)) or not windows
                or any(not isinstance(w, str) or w not in WINDOW_CLASSES
                       for w in windows)):
            problems.append(f"{where}.windows: must be a non-empty subset of "
                            f"{WINDOW_CLASSES}")
    if "max_request_tokens" in raw:
        mrt = raw["max_request_tokens"]
        if not _is_int(mrt) or mrt <= 0:
            problems.append(f"{where}.max_request_tokens: must be a positive int")
    for key in ("trains_on_data", "claude_family"):
        if key in raw and not isinstance(raw[key], bool):
            problems.append(f"{where}.{key}: must be a bool")
    if len(problems) > start:
        return None
    data = dict(raw)
    data.pop("expires", None)  # Old operator rows remain loadable, without local expiry.
    data["windows"] = tuple(windows)
    data["roles"] = tuple(roles)
    return Leg(**data)


def load_legs(path: Path | None) -> tuple[Leg, ...]:
    """Return ``DEFAULT_LEGS`` for ``None``, else the legs of the JSON file at ``path``.

    The file is ``{"legs": [{Leg fields}]}`` and fully replaces the defaults.  Raises
    ``ValueError`` listing every problem found.
    """
    if path is None:
        return DEFAULT_LEGS
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read legs file: {exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"legs file is not valid JSON: {exc}") from exc
    if not isinstance(doc, Mapping) or not isinstance(doc.get("legs"), list):
        raise ValueError('legs file must be an object {"legs": [...]}')
    problems: list[str] = []
    legs = [_build_leg(i, raw, problems) for i, raw in enumerate(doc["legs"])]
    if not doc["legs"]:
        problems.append("legs: must not be empty")
    ids = [leg.id for leg in legs if leg is not None]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"legs: duplicate id {dup!r}")
    if problems:
        raise ValueError("invalid legs file: " + "; ".join(problems))
    return tuple(legs)  # type: ignore[arg-type]


def with_measured(legs: Iterable[Leg], measured: Mapping[str, int]) -> tuple[Leg, ...]:
    """Return copies of ``legs`` with ``max_request_tokens`` taken from ``measured``.

    ``measured`` maps leg id to the measured window; ids not in ``legs`` are ignored.
    A measured value must be a positive int, otherwise ``ValueError``.
    """
    out = []
    for leg in legs:
        if leg.id in measured:
            value = measured[leg.id]
            if not _is_int(value) or value <= 0:
                raise ValueError(f"measured window for {leg.id!r} must be a positive int")
            leg = dataclasses.replace(leg, max_request_tokens=value)
        out.append(leg)
    return tuple(out)
