"""Route policy: which leg families and harnesses ``plan_spawn`` skips (OPERATOR D-3198).

Policy is data, not code: ``route_policy.json`` next to this module holds the
operator's current lists; ``PLANGRAPH_ROUTE_POLICY`` names a replacement file.

* ``banned_families`` -- never routed (kimi, deepseek);
* ``off_families`` -- switched off for now (glm);
* ``paused_harnesses`` -- ``{harness: {"allowed_roles": [...]}}``: a paused
  harness serves only the listed roles (qodercli: review seats only);
* ``review_tools_denied_legs`` -- review legs parked because the harness denies file tools;
* ``review_allowed_families`` -- families lifted for the *review* role only
  (D-3552: deepseek/glm for review seats; implement/orchestrate stay banned/off).
  kimi is refused here: it stays banned for every role (D-3084).

Skips are recorded in the plan with reason ``banned_family`` / ``off_family`` /
``paused_harness``.  Pure data and validation; the only I/O is reading the file.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from .legs import LEG_ROLES

__all__ = ["RoutePolicy", "load_route_policy", "DEFAULT_POLICY_PATH"]

DEFAULT_POLICY_PATH = Path(__file__).with_name("route_policy.json")
_KEYS = ("banned_families", "off_families", "paused_harnesses", "review_allowed_families",
         "review_tools_denied_legs")


def review_priority(leg) -> int:
    """D-4147 role order within a cost tier; no new family preferences."""
    return 2 if "haiku" in leg.model.lower() else 0 if leg.family == "mimo" else 1


def opencode_repository_review(leg):
    """OpenCode review seats with the bounded repository tool policy."""
    return (getattr(leg, 'tool_mode', None) == 'tools'
            and getattr(leg, 'harness', None) == 'opencode'
            and getattr(leg, 'id', None) in {
                'opencode-go-deepseek-v4-flash', 'opencode-go-glm-5.3-flash'})


def has_repository_review_tools(leg):
    """Identify review legs that can open files and run tests."""
    return leg.tool_mode == "tools" and (leg.harness != "opencode" or opencode_repository_review(leg))


def train_delta_document(card, role):
    """The verified replay brief is the entire input for this witnessed review."""
    return (role == "review" and isinstance(card, Mapping)
            and card.get("review_document") == "train_delta"
            and card.get("witness_required") is True
            and "diff_ref" not in card and not card.get("repository_review_required"))


def implement_key(leg, tier_idx, pos):
    """Eligible Sol xhigh then Gemini precede other cost tiers (rule 3.23a/D-5212).

    Remaining subscription legs precede pay-as-you-go; table order breaks ties.
    """
    from .legs import CODEX_FALLBACK_WRITERS, LEAD_WRITER, SONNET_WRITERS, WRITER_ORDER
    index = WRITER_ORDER.index(leg.id) if leg.id in WRITER_ORDER else len(WRITER_ORDER)
    last_codex = max(i for i, id in enumerate(WRITER_ORDER) if id.startswith("cx-gpt-"))
    if leg.id in SONNET_WRITERS:   # directly behind the Codex lead (only routed while the lead is limited)
        index = WRITER_ORDER.index(LEAD_WRITER) + 0.5
    elif leg.id in CODEX_FALLBACK_WRITERS:
        index = last_codex + 0.5
    return (leg.id != LEAD_WRITER, index, tier_idx, pos)


def implement_model_family(value):
    """Implement-only aliases must not change scoring or cross-family review."""
    from plangraph.routing_support.resolver import detect_model_family
    if any(alias in value.lower() for alias in ("anthropic", "fable")):
        return "claude"
    return detect_model_family(value)


def implement_family_excluded(leg):
    """No Claude identity or unidentified family may write automatically, even renamed."""
    from plangraph.routing_support.resolver import MODEL_FAMILIES
    from .legs import CODEX_FALLBACK_WRITERS, SONNET_WRITERS, spawnable_legs
    if leg.id in CODEX_FALLBACK_WRITERS or leg.id in SONNET_WRITERS:
        return False
    registered = {l.family for l in spawnable_legs() if l.model == leg.model}
    identities = {implement_model_family(value) for value in
                  (leg.id, leg.family, leg.model, leg.harness, *registered)}
    return (leg.claude_family or "claude" in identities
            or not identities.intersection(MODEL_FAMILIES | {"grok", "openrouter", "flint", "beam", "pickle"}))


@dataclass(frozen=True)
class RoutePolicy:
    banned_families: tuple[str, ...] = ()
    off_families: tuple[str, ...] = ()
    paused_harnesses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    review_allowed_families: tuple[str, ...] = ()
    review_tools_denied_legs: tuple[str, ...] = ()

    def allows(self, harness: str, role: str) -> bool:
        """False when ``harness`` is paused and ``role`` is not one it still serves."""
        allowed = self.paused_harnesses.get(harness)
        return allowed is None or role in allowed

    def skip_reason(self, leg, role: str) -> str | None:
        # Keeper families too: "zhipu" bans glm legs, "deepseek" every deepseek spelling.
        from .seats import seat_family

        # D-5113 lifts Muse admission; utility inputs choose among its routes.
        # Explicit audited --leg overrides do not use this automatic-route gate.
        if leg.family == "grok" or seat_family(leg.family) == "xai":
            return "off_family"

        def listed(families):
            return bool({leg.family, seat_family(leg.family)}
                        & ({seat_family(f) for f in families} | set(families)))
        # D-3552: review seats may use review_allowed_families even when the
        # family is banned/off.  Only those two checks are skipped: kimi is
        # never liftable (D-3084), and paused_harness / no_verdict_seat below
        # still apply.
        lifted = (role == "review" and listed(self.review_allowed_families)
                  and not listed(("kimi",)))
        if not lifted:
            if listed(self.banned_families):
                return "banned_family"
            if listed(self.off_families):
                return "off_family"
        if not self.allows(leg.harness, role):
            return "paused_harness"
        if role == "review" and leg.id in self.review_tools_denied_legs:
            return "opencode_tools_denied"
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
    review_allowed = _str_list(doc, "review_allowed_families", problems)
    tools_denied = _str_list(doc, "review_tools_denied_legs", problems)
    from .seats import seat_family
    if any(seat_family(f) == "kimi" for f in review_allowed):
        problems.append("review_allowed_families: kimi is never a seat (D-3084)")
    paused: dict[str, tuple[str, ...]] = {}
    raw = doc.get("paused_harnesses", {})
    if not isinstance(raw, Mapping):
        problems.append("paused_harnesses: must be an object")
        raw = {}
    for harness, entry in raw.items():
        roles = entry.get("allowed_roles") if isinstance(entry, Mapping) else None
        if (not isinstance(roles, list) or any(r not in LEG_ROLES for r in roles)
                or set(entry) - {"allowed_roles"}):
            problems.append(f"paused_harnesses.{harness}: needs allowed_roles, a subset of {LEG_ROLES}")
            continue
        paused[harness] = tuple(roles)
    if problems:
        raise ValueError("invalid route policy: " + "; ".join(problems))
    return RoutePolicy(banned, off, paused, review_allowed, tools_denied)


def codex_leg_effort(leg, card):
    """Apply the card's effort to a gateway GPT leg, or refuse unapproved escalation."""
    from plangraph.planner.legs import CX_EFFORTS
    from plangraph.routing_errors import SpawnRefused
    if not leg.model.startswith('omniroute/cx-gpt-'):
        return leg
    suffix = leg.model.rsplit('-', 1)[-1]
    pinned = suffix if suffix in CX_EFFORTS else None
    requested = card.get('effort')
    if requested is not None and requested not in CX_EFFORTS:
        raise SpawnRefused('effort_invalid: card.effort is not a supported GPT effort')
    effort = requested or pinned or leg.effort or 'medium'
    elevated = ('high', 'xhigh', 'max')
    if pinned in elevated or effort in elevated:
        decision = card.get('effort_decision')
        if (requested != effort or (pinned in elevated and pinned != requested)
                or not isinstance(decision, str) or not re.fullmatch(r'D-[0-9]+', decision)):
            raise SpawnRefused('effort_needs_decision: elevated GPT effort requires matching card.effort and card.effort_decision D-<n>')
    return replace(leg, effort=effort)


def refuse_card_sandbox(card):
    """Refuse a card that names a sandbox wider than the writer sandbox (workspace-write)."""
    from plangraph.routing_errors import SpawnRefused
    mode = card.get('sandbox')
    if mode is None or mode == 'workspace-write':
        return
    if mode == 'danger-full-access':
        raise SpawnRefused('sandbox_refused: danger-full-access is never granted to a spawned run; '
                           'the run gets workspace-write with the secret paths denied')
    raise SpawnRefused(f'sandbox_refused: card.sandbox {mode!r} is not supported; only workspace-write is granted')
