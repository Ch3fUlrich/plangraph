"""Planner kit: cards, readiness gate, routing, legs, context packs and the keeper glue.

The public names of every module are re-exported here; see ``docs/PLANNER-KIT.md``.
"""
from __future__ import annotations

from . import card, context_pack, legs, readiness, routing, service, start
from .card import (Card, CardError, apply_cards, cards_from_batch, load_cards_yaml,
                   parse_card, strip_cards)
from .context_pack import (CHARS_PER_TOKEN, Pack, PackFile, PackTooBig, build_pack,
                           estimate_tokens, pack_budget)
from .legs import DEFAULT_LEGS, HARNESSES, TIERS, Leg, load_legs, with_measured
from .readiness import (CODES, USABLE_FRACTION, WINDOW_TOKENS, Finding, Verdict,
                        check_card, check_cards, default_dry_run, paths_overlap,
                        write_sets_disjoint)
from .routing import (Route, RouteError, effective_estimate, next_after_failure,
                      pick_window_class, route_card)
from .service import (GATEABLE_STATES, GateResult, gate_card, import_batch,
                      pack_for_card, route_for_card, running_write_sets)
from .start import StartGate, fallback_providers, guard_start

__all__ = [
    "card", "context_pack", "legs", "readiness", "routing", "service", "start",
    "Card", "CardError", "apply_cards", "cards_from_batch", "load_cards_yaml",
    "parse_card", "strip_cards",
    "CHARS_PER_TOKEN", "Pack", "PackFile", "PackTooBig", "build_pack",
    "estimate_tokens", "pack_budget",
    "DEFAULT_LEGS", "HARNESSES", "TIERS", "Leg", "load_legs", "with_measured",
    "CODES", "USABLE_FRACTION", "WINDOW_TOKENS", "Finding", "Verdict", "check_card",
    "check_cards", "default_dry_run", "paths_overlap", "write_sets_disjoint",
    "Route", "RouteError", "effective_estimate", "next_after_failure",
    "pick_window_class", "route_card",
    "GATEABLE_STATES", "GateResult", "gate_card", "import_batch", "pack_for_card",
    "route_for_card", "running_write_sets",
    "StartGate", "fallback_providers", "guard_start",
]
