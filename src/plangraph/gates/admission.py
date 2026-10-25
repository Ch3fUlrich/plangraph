"""Spawner admission for the D-2739 §0 gates (writer and fixer runs only).

``admit(card, role, prompt, item)`` returns the (possibly rebuilt) prompt and a
list of notes, or raises :class:`GateRefused`.  *item* is the keeper ``show``
row of ``card['key']`` (``None`` when no keeper is configured: item-state gates
are then skipped and noted, design invariant I5).

Card fields read: ``key``, ``findings`` (``[{id?, text, severity?, files?}]``),
``seat_outputs`` (paths of seat reply files: findings are parsed by code),
``invariants`` (design invariant table text), ``l1_note`` (appended to a fix
brief), ``writer_model`` (the lane writer, for the fixer floor), ``est_lines``.
"""
from __future__ import annotations

import re

from plangraph.routing_errors import SpawnRefused

from . import findings as F
from .models import fixer_floor, model_tier
from .rounds import ROUND_CAP

__all__ = ["GateRefused", "WRITER_ROLES", "PLAN_LINES", "is_fix", "admit", "filter_fixer_legs"]

WRITER_ROLES = frozenset({"implement", "fix", "writer"})
PLAN_LINES = 400
_MERGE_MAIN = re.compile(
    r"\b(?:git\s+)?(?:merge|rebase|pull)\b[^\n]{0,40}?\b(?:origin/)?(?:main|master)\b", re.I)


class GateRefused(SpawnRefused):
    """A D-2739 gate refused the spawn; the message names the gate."""


def is_fix(card, role):
    return role in WRITER_ROLES and bool(
        card.get("findings") or card.get("seat_outputs") or role == "fix")


def _card_findings(card):
    out = []
    for n, raw in enumerate(card.get("findings") or (), 1):
        if isinstance(raw, str):
            raw = {"text": raw}
        text = raw.get("text") or raw.get("description") or ""
        out.append({"id": str(raw.get("id") or f"card-F{n}"), "severity": raw.get("severity", "?"),
                    "text": text, "files": raw.get("files") or ()})
    if card.get("seat_outputs"):
        out.extend(F.findings_from_files(card["seat_outputs"], card=card))
    return out


def _design_ok(gates):
    design = (gates or {}).get("design") or {}
    return bool(design.get("ref") and design.get("reviewed_by"))


def card_findings(card):
    """The card's findings plus the findings parsed from its seat output files."""
    return _card_findings(card)


def admit(card, role, prompt, item=None):
    """Apply the gates; return ``(prompt, notes)`` or raise :class:`GateRefused`."""
    card = card or {}
    notes = []
    if role not in WRITER_ROLES:
        return prompt, notes
    if _MERGE_MAIN.search(prompt or ""):
        raise GateRefused("base-pin (§0.2f): writer/fixer briefs never merge or rebase main; "
                          "the L1 merges main once, right before the final review")
    if item is None:
        if card.get("key"):
            notes.append("gates_skipped: no keeper item (round cap, plan gate)")
    else:
        gates = item.get("gates") or {}
        since = int(gates.get("since_replan") or 0)
        if since >= ROUND_CAP:
            raise GateRefused(
                f"round-cap (§0.2e): {item.get('key')} had {since} review rounds since the last "
                f"replan; attach a replan (log_item kind=replan, strong model) first")
        big = int(card.get("est_lines") or 0) > PLAN_LINES
        if (item.get("review_tier") == "high" or big) and not _design_ok(gates):
            raise GateRefused(
                f"plan-gate (§0.2a): {item.get('key')} is "
                f"{'HIGH' if item.get('review_tier') == 'high' else f'> {PLAN_LINES} lines'}"
                "; log a reviewed design first (log_item kind=design ref=... reviewed_by=...)")
    if is_fix(card, role):
        found = _card_findings(card)
        if not found:
            raise GateRefused("fix-brief (§0.2d): a fix run needs findings or seat_outputs")
        invariants = card.get("invariants") or ""
        if not invariants and item is not None:
            invariants = (((item.get("gates") or {}).get("design") or {}).get("invariants") or "")
        built = F.build_fix_brief(found, invariants, note=prompt or card.get("l1_note") or "")
        missing = F.missing_ids(built, found)
        if missing:  # invariant I4: only reached if build_fix_brief regresses
            raise GateRefused(f"fix-brief (§0.2d): brief misses finding ids {missing}")
        notes.append(f"fix brief built by code: {len(found)} finding(s)")
        prompt = built
    return prompt, notes


def check_brief(brief, card):
    """Ids of the card's findings that *brief* misses (for hand-written briefs)."""
    return F.missing_ids(brief, _card_findings(card))


def filter_fixer_legs(legs, card, role, item=None):
    """Drop fixer legs that are flash class or weaker than the lane writer (§0.2a/d).

    *legs* have a ``model`` attribute or key.  Returns ``(kept, dropped)``.
    """
    if not is_fix(card or {}, role):
        return list(legs), []
    writer = (card or {}).get("writer_model") or (item or {}).get("writer_model")
    floor = fixer_floor(writer)
    kept, dropped = [], []
    for leg in legs:
        model = getattr(leg, "model", None) or (leg.get("model") if isinstance(leg, dict) else None)
        (kept if model_tier(model) >= floor else dropped).append(leg)
    return kept, dropped
