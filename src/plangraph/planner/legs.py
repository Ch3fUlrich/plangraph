"""Routing legs: the ordered model/harness combos the keeper may hand a card to (D25).

A *leg* is one concrete way to run a card: a harness, a model, the window classes it
serves and the largest request it accepts.  ``DEFAULT_LEGS`` lists them in the D25
preference order (position = preference).  The numbers are defaults; the measured
windows kept in the registry override them via :func:`with_measured`.  An operator
file (``{"legs": [...]}``) replaces the whole list via :func:`load_legs`.

Tiers run cheapest first: ``trial``, ``free``, ``subscription``, ``paid``.  The trial
tier is empty by default.  Pure data and validation; the only I/O is reading the
optional JSON override file and the client probe of own-harness routing (PATH lookup).
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from plangraph.keeper.schema import WINDOW_CLASSES
from plangraph.mimo_policy import MimoPathError, check_mimo_path

__all__ = ["OWN_HARNESS", "CLIENT_MISSING", "HarnessChoice", "client_installed", "resolve_harness",
           "resolve_legs", "own_harness_legs", "harness_skips", "refuse_foreign_pin", "own_harness_enabled", "service_probe",
           "TIERS", "HARNESSES", "TOOL_MODES", "Leg", "DEFAULT_LEGS", "WRITER_ORDER", "leg_active",
           "load_legs", "with_measured", "EXPLICIT_LEGS", "EXPLICIT_WRITERS", "spawnable_legs",
           "CX_EFFORTS"]

#: Cost tiers, cheapest first.
TIERS = ("trial", "free", "subscription", "paid")
LEG_ROLES = ("implement", "review", "orchestrate", "research")
#: Harness names a leg may run under.
HARNESSES = ("qodercli", "opencode", "agy", "claude", "codex")
#: How a review leg reads the change: with its own tools, or only from a diff the
#: spawner inlines into the prompt (D-3134/D-3173: luna, mimo-pro give no verdict
#: in tool mode).
TOOL_MODES = ("tools", "inline-only", "prompt-only")
#: Families whose models read images; a text-only leg fails a card that needs vision.
VISION_FAMILIES = frozenset({"gpt", "openai", "claude", "gemini"})
#: What a card can need of a leg (``card["needs"]``); ``card["tool_mode"]`` names a minimum tool mode.
CARD_NEEDS = ("tools", "shell", "long_context", "vision")


def unmet_need(leg, card):
    """The first card need the leg lacks (``tools``, ``shell``, ``long_context``, ``vision``, ``tool_mode``), or None."""
    for need in card.get("needs") or ():
        if need in ("tools", "shell"):   # a shell runs only where the leg has its own tools
            ok = leg.tool_mode == "tools"
        elif need == "long_context":
            ok = "1m" in leg.windows
        else:
            ok = leg.vision if leg.vision is not None else leg.family in VISION_FAMILIES
        if not ok:
            return need
    required = card.get("tool_mode")
    if required is not None and TOOL_MODES.index(leg.tool_mode) > TOOL_MODES.index(required):
        return "tool_mode"
    return None
#: Models that never sit as a review seat, routed or hand-picked (D-3225:
#: opencode-go/qwen3.8-flash via OmniRoute returned empty content 6/6, ~289 s).
NO_VERDICT_SEATS = frozenset({"opencode-go/qwen3.8-flash"})
# Advisory only: these models remain routable, but explicit reviews warn.
REVIEW_UNFIT_SEATS = frozenset({"mimo/mimo-v2.6-flash"})
#: Reasoning-effort suffixes OmniRoute's cx provider serves (/v1/models, 2026-11-09).
CX_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _registry_limit(provider, model):
    from plangraph.routing_support.opencode_overlay import load_root_providers
    shipped = Path(__file__).resolve().parents[3] / 'configuration/opencode/providers.json'
    models = (load_root_providers(shipped) or {}).get(provider, {}).get('models', {})
    return models.get(model, {}).get('limit', {})


def _codex_context(model):
    return _registry_limit('omniroute', model).get('context', 1_000_000)


#: Bound while the registry file is absent or has no entry for the model (a copied tree
#: without ``configuration/``); the registry logs the missing file and routing stays safe.
_NO_REGISTRY_REQUEST_TOKENS = 100_000


def _registry_request_tokens(provider, model):
    """Largest request an opencode leg takes: the registry context minus its output reserve.

    The provider registry (``configuration/opencode/providers.json``) is the one truth for
    a model's window; ``legs.py`` types no limit for a model the registry lists.
    """
    limit = _registry_limit(provider, model)
    if not isinstance(limit.get('context'), int):
        return _NO_REGISTRY_REQUEST_TOKENS
    return limit['context'] - limit.get('output', 0)


_SOL_CONTEXT = _codex_context('cx-gpt-6.2-sol')


@dataclass(frozen=True)
class Leg:
    """One routing leg (see module docstring)."""

    id: str
    tier: str
    harness: str
    model: str
    family: str
    windows: tuple[str, ...]
    max_request_tokens: int | None
    trains_on_data: bool | None
    claude_family: bool = False
    note: str = ""
    roles: tuple[str, ...] = ("orchestrate",)
    tool_mode: str = "tools"
    disabled: str | None = None
    expires: float | None = None
    effort: str | None = None
    benchmark_optional: bool = False  # Missing data skips this leg in strict routes.
    #: Why the model's own harness was not used (``client_missing``); None when it was.
    harness_fallback: str | None = None
    web_access: bool = False
    #: Reads images; None takes the family default (:data:`VISION_FAMILIES`).
    vision: bool | None = None

    @property
    def has_web_tools(self) -> bool:
        return self.web_access and self.tool_mode == "tools"


def _leg(id, tier, harness, model, family, windows, max_req, trains, claude=False,
         note=""):
    return Leg(id, tier, harness, model, family, tuple(windows), max_req, trains,
               claude, note)


DEFAULT_LEGS: tuple[Leg, ...] = (
    _leg("qodercli-qwen3.8-flash", "free", "qodercli", "Qwen3.8-Flash", "qwen",
         ("128k", "256k"), 100000, False,
         note="free while qoder credits remain, until its rate limit"),
    _leg("mimo-v2.6-flash", "subscription", "opencode",
         "mimo/mimo-v2.6-flash", "mimo", ("128k", "256k", "1m"),
         _registry_request_tokens("mimo", "mimo-v2.6-flash"), False),
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
        "opencode/deepseek-v4.1-flash-free", "deepseek", ("128k",), 200000, True,
        roles=("review", "implement")),
    Leg("opencode-glm-5.3-flash-free", "free", "opencode",
        "opencode/glm-5.3-flash-free", "glm", ("128k",), 100000, True,
        roles=("review", "implement"),
        disabled="D-6310: opencode/glm-5.3-flash-free not served by opencode models (use opencode-go/glm-5.3-flash); never remove (D-3828)"),
    Leg("openrouter-free", "free", "opencode", "openrouter/free", "openrouter",
        ("128k",), 100000, True, roles=("implement",)),
    Leg("cx-gpt-6.2-sol", "subscription", "codex", "omniroute/cx-gpt-6.2-sol",
        "gpt", WINDOW_CLASSES, _SOL_CONTEXT, False, roles=("implement",),
        note="D-6310: operator implement leg (ChatGPT subscription via OmniRoute cx)"),
    Leg("cx-gpt-6.2-astra", "subscription", "codex", "omniroute/cx-gpt-6.2-astra",
        "gpt", WINDOW_CLASSES, _codex_context('cx-gpt-6.2-astra'), False, roles=("implement",),
        note="D-6310: operator implement leg (GPT-6.2 Astra via OmniRoute cx)"),
    Leg("cx-gpt-6-luna", "subscription", "codex", "omniroute/cx-gpt-6-luna",
        "gpt", ("128k", "256k"), 475000, False, roles=("review",),
        tool_mode="inline-only"),
    *(Leg(f"agy-gemini-4-hydrogen-{effort}", "subscription", "agy",
          f"gemini-4-hydrogen-{effort}", "gemini", ("128k", "256k", "1m"), 900000, False,
          roles=("implement", "review", "research") if effort == "high" else (role, "research"),
          effort=effort, tool_mode="tools",
          note="D-5903: enabled; agy models lists Hydrogen low/medium/high/xhigh (2026-11-14); "
               "agy CLI review verdicts: PR #878 (three heads), #886 (2026-11-14), "
               "handoffs/plan-order/reviews/{pr878-5ac07d1d,pr886-75c5bd2f}-hydrogen.txt. "
               "Tier/privacy/windows/request bound inherit agy Claude 5.5: same subscription/harness; "
               "clients.launch_stdin_payload carries stream JSON on stdin, not prompt argv. "
               "Review plan mode reads the working tree with tools: plan-order file-marker probe 2026-11-14 09:5xZ. "
               "Seat unwitnessed: agy emits no served-model telemetry (rule 3.26).")
      for effort, role in (("high", "implement"), ("medium", "review"))),
    Leg("agy-gemini-4-neon-medium", "subscription", "agy", "gemini-4-neon-medium",
        "gemini", ("128k", "256k", "1m"), 900000, False, roles=("review",), effort="medium",
        tool_mode="tools", note="D-6310: replaces retired gemini-4-argon for review"),
    Leg("opencode-flint-40b-free", "free", "opencode", "opencode/flint-40b-free",
        "flint", ("128k",), 100000, True, roles=("implement", "review")),
    Leg("opencode-big-pickle", "free", "opencode", "opencode/big-pickle",
        "glm", ("128k",), 100000, True, roles=("implement", "review")),
    Leg("agy-gpt-oss-120b", "subscription", "agy", "gpt-oss-120b-medium",
        "openai", ("128k",), 100000, False, roles=("review",), benchmark_optional=True),
    _replace(_legacy["mimo-v2.6-flash"], roles=("implement", "review"), tool_mode="inline-only"),
    Leg("mimo-v2.6-pro", "subscription", "opencode", "mimo/mimo-v2.6-pro",
        "mimo", ("128k", "256k", "1m"), _registry_request_tokens("mimo", "mimo-v2.6-pro"), False,
        roles=("implement", "review", "orchestrate"), tool_mode="inline-only"),
    _replace(_legacy["agy-sonnet-5.5"], roles=("orchestrate", "review")),
    Leg("agy-claude-opus", "subscription", "agy", "claude-opus-4-6-thinking",
        "claude", ("128k", "256k"), 180000, False, claude_family=True, roles=("review",)),
    Leg("agy-opus-5.5", "subscription", "agy", "claude-opus-5-5-high",
        "claude", ("128k", "256k", "1m"), 900000, False, claude_family=True,
        roles=("review", "research")),
    Leg("omniroute-deepseek-flash", "subscription", "opencode", "omniroute/deepseek-v4.1-flash",
        "deepseek", ("128k",), 100000, False, roles=("implement", "review", "orchestrate")),
    Leg("omniroute-muse-spark", "subscription", "opencode",
        "omniroute/spark-1.3-contributor", "muse", ("128k",), 100000, False,
        roles=("implement", "review", "orchestrate")),
    Leg("agy-opus", "subscription", "agy", "claude-opus-5-5-medium", "claude",
        ("128k",), 100000, False, claude_family=True, roles=("review", "orchestrate")),
    Leg("opencode-go-beam-free", "free", "opencode", "opencode-go/beam-free",
        "beam", ("128k",), 100000, False, roles=("implement", "review")),
    Leg("opencode-go-glm-5.3-flash", "subscription", "opencode",
        "opencode-go/glm-5.3-flash", "glm", ("128k", "256k", "1m"), 900000, False,
        roles=("review", "implement", "research"), web_access=True),
    Leg("opencode-go-qwen3.8-max", "subscription", "opencode",
        "opencode-go/qwen3.8-max", "qwen", ("128k",), 100000, False,
        roles=("review",)),
    Leg("omniroute-grok-4.7", "subscription", "opencode", "omniroute/gc/grok-4.7",
        "grok", ("128k", "256k"), 120000, False, roles=("review",), tool_mode="tools",
        note="D-3701: grok review seat via OmniRoute gc"),
    Leg("opencode-go-muse-spark", "subscription", "opencode",
        "opencode-go/muse-spark-1.3-contributor", "muse", ("128k",), 100000,
        False, roles=("review",)),
    Leg("opencode-console-muse-spark", "subscription", "opencode",
        "console-meta/muse-spark-1.3-contributor", "muse", ("128k",), 100000,
        False, roles=("review",),
        note="D-4280: explicit audited selection; scrubbed or public material only"),
    Leg("opencode-go-deepseek-v4-flash", "subscription", "opencode",
        "opencode-go/deepseek-v4.1-flash", "deepseek", ("128k", "256k", "1m"), 900000,
        False, roles=("review", "implement", "research"), web_access=True),
    _replace(_legacy["deepseek"], model="omniroute/deepseek-direct-flash", roles=("implement",)),
    Leg("claude-haiku-5.5", "subscription", "claude", "claude-haiku-5-5", "claude",
        ("128k", "256k"), 180000, False, claude_family=True, roles=("review",),
        tool_mode="prompt-only",
        note="D-3605/D-3619: review fallback via Claude subscription; never seats a claude/anthropic writer"),
)

# D-6550 (rule 3.66): Hydrogen reviews in its own harness; writer-family exclusion still applies.
REVIEW_MIMO_PRO, REVIEW_MIMO_FLASH = "mimo-v2.6-pro", "mimo-v2.6-flash"
REVIEW_HYDROGEN, REVIEW_NEON = "agy-gemini-4-hydrogen-high", "agy-gemini-4-neon-medium"
REVIEW_ARGON_LOW, REVIEW_ARGON = "agy-gemini-4-argon-low", "agy-gemini-4-argon-medium"
REVIEW_DEEPSEEK, REVIEW_BEAM = "opencode-deepseek-v4.1-flash-free", "opencode-go-beam-free"
REVIEW_SONNET = "claude-sonnet-5-5-high"
REVIEW_GPT = "gpt-6.2-sol-medium"
# D-6652 (rule 3.66 (f)): every head needs these two OpenCode seats with file access, each
# outside every writer family of the diff; both must ACCEPT with FILES READ.  They lead the route.
REVIEW_REQUIRED = ("opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash")
REVIEW_ORDER = (*REVIEW_REQUIRED, REVIEW_HYDROGEN, REVIEW_GPT, REVIEW_MIMO_PRO, REVIEW_DEEPSEEK,
                REVIEW_NEON, REVIEW_SONNET, REVIEW_MIMO_FLASH, REVIEW_ARGON_LOW, REVIEW_ARGON)
LOW_RISK_ONLY_LEGS = frozenset({"opencode-flint-40b-free", "opencode-big-pickle", "opencode-go-beam-free"})
REVIEW_FALLBACK_LEGS = (
    Leg(REVIEW_GPT, "subscription", "codex", "gpt-6.2-sol-medium", "gpt",
        ("128k", "256k"), 475000, False, roles=("review",), effort="medium", tool_mode="tools",
        note="D-6481 rule 3.61: native Codex review run with repository access; a gpt-written diff skips it"),
    Leg(REVIEW_ARGON_LOW, "subscription", "agy", "gemini-4-argon-low", "gemini",
        ("128k", "256k", "1m"), 900000, False, roles=("review", "research"), effort="low",
        tool_mode="tools", note="D-6366: review leg, last since rule 3.61 (D-6481); retired for implement (D-6310)"),
    Leg(REVIEW_ARGON, "subscription", "agy", "gemini-4-argon-medium", "gemini",
        ("128k", "256k", "1m"), 900000, False, roles=("review", "research"), effort="medium",
        tool_mode="tools", note="D-6366: same-family fallback after argon-low; retired for implement (D-6310)"),
    Leg("console-deepseek-flash", "free", "opencode", "console-deepseek/deepseek-flash", "deepseek",
        ("128k",), 100000, False, roles=("review",)),
    Leg(REVIEW_SONNET, "subscription", "claude", "claude-sonnet-5-5-high", "claude",
        ("128k", "256k", "1m"), 900000, False, claude_family=True,
        roles=("review", "implement", "research"), effort="high",
        note="D-6652: implement only while the Codex lead is limited; review only for credential diffs or when both required seats are out"),
)
#: D-6652 (operator correction 2026-11-23): the Sonnet writer legs, eligible only while the Codex lead is
#: limited. ``claude-sonnet-5-5-xhigh`` serves cards marked ``hard``.
SONNET_WRITER, SONNET_WRITER_HARD = REVIEW_SONNET, "claude-sonnet-5-5-xhigh"
SONNET_WRITERS: tuple[str, ...] = (SONNET_WRITER, SONNET_WRITER_HARD)
CLAUDE_WRITER_LEGS = (
    Leg(SONNET_WRITER_HARD, "subscription", "claude", "claude-sonnet-5-5-xhigh", "claude",
        ("128k", "256k", "1m"), 900000, False, claude_family=True,
        roles=("implement",), effort="xhigh", note="D-6652: Sonnet xhigh for cards marked hard"),
)

# Preserve role-neutral keeper admission before applying implement-only policy.
READINESS_LEGS = tuple(_replace(leg, roles=tuple(r for r in leg.roles if r not in ('implement', 'research')),
                               disabled=leg.disabled or (
                                   'D-3828: writer disabled; no retained review or orchestrate role'
                                   if leg.roles == ('implement',) else None))
                       if leg.id != 'cx-gpt-6.2-sol' else leg for leg in DEFAULT_LEGS)

# Rule 3.53 (OPERATOR D-6310): implement legs priority and opencode implementation fallback.
# The Sonnet writers (SONNET_WRITERS) sit outside this order: they serve only while the Codex lead is limited.
WRITER_ORDER: tuple[str, ...] = (
    "cx-gpt-6.2-sol-medium",
    "cx-gpt-6.2-astra",
    "agy-gemini-4-hydrogen-high",
    "opencode-deepseek-v4.1-flash-free",
    "opencode-go-deepseek-v4-flash",
    "opencode-go-glm-5.3-flash",
)
LEAD_WRITER = WRITER_ORDER[0]

#: Operator 2026-11-09: Claude 5.5 seats and effort variants, spawnable by an
#: explicit ``--leg`` pick (or the model override) only.  They are NOT in
#: DEFAULT_LEGS, except Sol xhigh promoted below (D-5206); Claude is never an
#: automatic writer; ``SpawnService.route``
#: appends them as ``explicit_only`` skips, ``plan_spawn`` does not.  Model ids are what ``agy models`` and OmniRoute
#: /v1/models served that day (agy serves no Sonnet xhigh, cx no -ultra).  Claude
#: legs are claude-family, so the D-2511 fence keeps them off claude-written work.
#: A cx effort model needs a per-run opencode entry (``opencode_render``).
EXPLICIT_LEGS: tuple[Leg, ...] = (
    *(Leg(f"agy-claude-{m}-5-5-{e}", "subscription", "agy", f"claude-{m}-5-5-{e}",
          "claude", ("128k", "256k", "1m"), 900000, False, claude_family=True,
          roles=("review", "implement"), effort=e, note="operator 2026-11-09: Antigravity quota")
      for m in ("sonnet", "opus") for e in ("low", "medium", "high")),
    *(Leg(f"cx-gpt-6.2-sol-{e}", "subscription", "codex", f"omniroute/cx-gpt-6.2-sol-{e}",
          "gpt", WINDOW_CLASSES, _SOL_CONTEXT, False, roles=("implement",), effort=e)
      for e in CX_EFFORTS),
    *(Leg(f"cx-gpt-6-luna-{e}", "subscription", "codex", f"omniroute/cx-gpt-6-luna-{e}",
          "gpt", ("128k", "256k"), 475000, False, roles=("review",),
          tool_mode="inline-only")
      for e in CX_EFFORTS),
)
EXPLICIT_LEGS += REVIEW_FALLBACK_LEGS + CLAUDE_WRITER_LEGS

# Promote the existing effort leg; retain plain Sol as an active explicit-only pick.
_plain_sol = next(leg for leg in DEFAULT_LEGS if leg.id == "cx-gpt-6.2-sol")
_sol_default = next(leg for leg in EXPLICIT_LEGS if leg.id == LEAD_WRITER)
_sol_default = _replace(_sol_default, roles=(*_sol_default.roles, "research"), web_access=True)
DEFAULT_LEGS = tuple(_sol_default if leg.id == _plain_sol.id else leg for leg in DEFAULT_LEGS)
EXPLICIT_LEGS = (_plain_sol, *(leg for leg in EXPLICIT_LEGS if leg.id != LEAD_WRITER))
# Keeper readiness keeps its existing admission, but uses the current routed id.
READINESS_LEGS = tuple(_replace(_sol_default, roles=_plain_sol.roles)
                       if leg.id == _plain_sol.id else leg for leg in READINESS_LEGS)
#: D-6550 permits these Claude writers when GPT Sol and Hydrogen are rate-limited.
CODEX_FALLBACK_WRITERS: tuple[str, ...] = ("agy-claude-sonnet-5-5-high",)
#: Explicit legs the spawn service admits for ``implement`` besides LEAD_WRITER.
EXPLICIT_WRITERS: frozenset[str] = frozenset(
    leg.id for leg in EXPLICIT_LEGS if "implement" in leg.roles)

# Order-only declarations: unknown capabilities stay disabled and role-less.
# JSON overrides still require all capability fields, even for declarations.
EXPLICIT_LEGS += tuple(
    Leg(id, 'subscription', harness, model, family, (), None, None,
        claude_family=family == 'claude', roles=(), effort=effort,
        disabled='D-5696: capability evidence required')
    for id, harness, model, family, effort in (
        ('muse-ember-max', 'opencode', 'omniroute/muse-ember-max', 'muse', 'max'),
        ('claude-fable-5.2-high', 'claude', 'claude-fable-5-2', 'claude', 'high')))


def spawnable_legs() -> tuple[Leg, ...]:
    """Every leg an explicit pick may name: the routed legs first, so a shared model
    resolves to its DEFAULT_LEGS leg.  Read at call time (callers and tests rebind
    DEFAULT_LEGS)."""
    return (*DEFAULT_LEGS, *EXPLICIT_LEGS)


def explicit_only_skips(role: str) -> tuple[dict, ...]:
    """`route` output rows for the explicit legs serving ``role``: listed, never routed."""
    return tuple({"leg": leg.id, "reason": "explicit_only", "evidence": {
        "kind": "explicit_only", "reason": "spawn with --leg " + leg.id}}
        for leg in EXPLICIT_LEGS if role in leg.roles
        and not (role == "review" and leg.id in REVIEW_ORDER)
        and not (role == "research" and leg in REVIEW_FALLBACK_LEGS)
        and not (role == "implement" and leg.id in (*CODEX_FALLBACK_WRITERS, *SONNET_WRITERS)))

_FIELDS = tuple(f.name for f in dataclasses.fields(Leg))
_REQUIRED = ("id", "tier", "harness", "model", "family", "windows",
             "max_request_tokens", "trains_on_data")
_STR_FIELDS = ("id", "tier", "harness", "model", "family", "note", "tool_mode")


def leg_active(leg: Leg, now=None) -> bool:
    """Temporary operator stops expire; permanent safety exclusions do not."""
    return leg.disabled is None or (leg.expires is not None and
                                   (time.time() if now is None else now) >= leg.expires)


def review_route_pool(base=None) -> tuple[Leg, ...]:
    """Candidate pool of the native review route: the routed legs plus the review fallbacks."""
    return (*(DEFAULT_LEGS if base is None else base), *REVIEW_FALLBACK_LEGS)


def research_route_pool(legs) -> tuple[Leg, ...]:
    """Research uses declared legs and reserves headroom on the default Sol leg."""
    if legs is not DEFAULT_LEGS:
        return tuple(leg for leg in legs if "research" in leg.roles)
    return tuple(_replace(leg, max_request_tokens=min(leg.max_request_tokens, 900_000))
                 if leg.id == LEAD_WRITER else leg
                 for leg in review_route_pool(legs) if "research" in leg.roles)


def routed_review_legs() -> list[Leg]:
    """Active legs the native review route can offer, in REVIEW_ORDER."""
    from .route_policy import load_route_policy
    denied = set(load_route_policy().review_tools_denied_legs)
    pool = {leg.id: leg for leg in review_route_pool()}
    return [pool[leg_id] for leg_id in REVIEW_ORDER
            if leg_id not in denied and leg_id in pool and leg_active(pool[leg_id])]


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _build_leg(index: int, raw, problems: list[str]) -> Leg | None:
    where = f"legs[{index}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be an object")
        return None
    from plangraph.retired_models import check_retired
    start = len(problems)
    try:
        check_retired(raw)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
    if isinstance(raw.get("model"), str):
        try:
            check_mimo_path(raw["model"])
        except MimoPathError as exc:
            problems.append(f"{where}.model: {exc}")
    for key in raw:
        if key not in _FIELDS:
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
    if raw.get("effort") is not None and raw["effort"] not in CX_EFFORTS:
        problems.append(f"{where}.effort: must be one of {CX_EFFORTS} or null")
    expiry = raw.get('expires')
    if expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)):
        problems.append(f"{where}.expires: must be a finite epoch timestamp")
    if disabled is not None and (not isinstance(disabled, str) or not disabled.strip()):
        problems.append(f"{where}.disabled: must be a non-empty reason string")
    roles = raw.get("roles", ("orchestrate",))
    if (not isinstance(roles, (list, tuple))
            or (not roles and not (isinstance(disabled, str) and disabled.strip()))
            or any(not isinstance(r, str) or r not in LEG_ROLES for r in roles)
            or len(set(roles)) != len(roles)):
        problems.append(f"{where}.roles: must be a subset of {'|'.join(LEG_ROLES)}")
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
    for key in ("trains_on_data", "claude_family", "benchmark_optional", "web_access"):
        if key in raw and not isinstance(raw[key], bool):
            problems.append(f"{where}.{key}: must be a bool")
    if raw.get("vision") is not None and not isinstance(raw["vision"], bool):
        problems.append(f"{where}.vision: must be a bool or null")
    if len(problems) > start:
        return None
    data = dict(raw)
    data["windows"] = tuple(windows)
    data["roles"] = tuple(roles)
    return Leg(effort=data.pop("effort", None), **data)


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


# Own-harness-first routing (rule 3.53, D-6310; operator 2026-11-20): a model runs in the
# harness it is trained on, OpenCode serves it only while that client is not installed on the
# host, and the leg then says why (``harness_fallback``).  The only I/O is the default probe.
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
    """The harness a leg runs in; ``leg`` is the leg to run (None: no usable harness on this host)."""

    harness: str | None
    fallback: str | None = None
    leg: Leg | None = None


def own_harness_enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() != "off"


def client_installed(name: str) -> bool:
    if name == "codex":
        from plangraph.routing_support.clients import resolve_codex_client
        return resolve_codex_client()[0] is not None
    from plangraph.routing_support.clients import resolve_harness_binary
    return resolve_harness_binary(name).path is not None


def service_probe(service) -> Callable[[str], bool] | None:
    """The client probe of a spawn service: its own ``client_probe``, else the host's PATH, or None
    (own-harness routing off) while ``PLANGRAPH_OWN_HARNESS=off``."""
    probe = getattr(service, "client_probe", None)
    if probe is not None:
        return probe
    return client_installed if own_harness_enabled() else None


def opencode_counterpart(leg: Leg) -> Leg | None:
    """The registered OpenCode leg serving the same gpt model (its ``omniroute/cx-`` alias), review-packaged."""
    if leg.family != "gpt":
        return None
    model = "omniroute/cx-" + leg.model.removeprefix("omniroute/").removeprefix("cx-")
    if not any(known.model == model for known in (*DEFAULT_LEGS, *EXPLICIT_LEGS)):
        return None
    tool_mode = "inline-only" if "implement" not in leg.roles else leg.tool_mode
    # The OpenCode leg is bound by the OmniRoute registry window, not by the native client's.
    key = model.removeprefix("omniroute/")
    base = next((key[:-len(e) - 1] for e in CX_EFFORTS if key.endswith("-" + e)), key)
    limit = _registry_limit("omniroute", key) or _registry_limit("omniroute", base)   # effort variants share the base window
    cap = limit["context"] - limit.get("output", 0) if isinstance(limit.get("context"), int) else leg.max_request_tokens
    if leg.max_request_tokens is not None:
        cap = min(cap, leg.max_request_tokens)
    return dataclasses.replace(leg, harness="opencode", model=model, tool_mode=tool_mode,
                               harness_fallback=None, web_access=False, max_request_tokens=cap)


def resolve_harness(leg: Leg, installed: Callable[[str], bool] | None = client_installed) -> HarnessChoice:
    """The harness a leg runs in: its family's own client when installed; else the OpenCode leg of the
    same model with ``client_missing``; else no usable leg.  ``installed=None`` switches the rule off."""
    harness = getattr(leg, "harness", None)
    own = OWN_HARNESS.get(getattr(leg, "family", None))
    # An inline-only leg is packaged for its OpenCode child; its model name is not the client's.
    if installed is None or not own or (getattr(leg, "tool_mode", "tools") != "tools" and harness not in own):
        return HarnessChoice(harness, None, leg)
    if harness in own:
        if installed(harness):
            return HarnessChoice(harness, None, leg)
        fallback = opencode_counterpart(leg)
        if fallback is None:
            return HarnessChoice(None, CLIENT_MISSING)
        return HarnessChoice("opencode", CLIENT_MISSING, dataclasses.replace(fallback, harness_fallback=CLIENT_MISSING))
    if installed(own[0]):
        return HarnessChoice(own[0], None, dataclasses.replace(leg, harness=own[0], web_access=False))
    return HarnessChoice(harness, CLIENT_MISSING, dataclasses.replace(leg, harness_fallback=CLIENT_MISSING))


def resolve_legs(legs: Iterable[Leg], installed: Callable[[str], bool] | None = client_installed
                 ) -> tuple[tuple[Leg, ...], tuple[dict, ...]]:
    """One resolution per leg: the legs moved into their own harness (or onto their OpenCode fallback),
    and the `route` skip rows of the legs with no usable harness on this host."""
    kept, skips = [], []
    for leg in legs:
        choice = resolve_harness(leg, installed)
        if choice.leg is not None:
            kept.append(choice.leg)
        else:
            skips.append({"leg": leg.id, "reason": CLIENT_MISSING, "evidence": {
                "kind": CLIENT_MISSING, "harness": leg.harness}})
    return tuple(kept), tuple(skips)


def own_harness_legs(legs: Iterable[Leg], installed: Callable[[str], bool] | None = client_installed) -> tuple[Leg, ...]:
    """The legs of :func:`resolve_legs`."""
    return resolve_legs(legs, installed)[0]


def harness_skips(legs: Iterable[Leg], installed: Callable[[str], bool] | None = client_installed) -> tuple[dict, ...]:
    """The skip rows of :func:`resolve_legs`."""
    return resolve_legs(legs, installed)[1]


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
