"""§0.2a/d model strength: fixers never flash, never weaker than the writer.

Three coarse tiers read from the model id: 0 flash class, 1 standard, 2 strong.
An unknown id is standard, never strong (design invariant I1).
"""
from __future__ import annotations

import re

__all__ = ["FLASH", "STANDARD", "STRONG", "RETIRED_FAMILIES", "model_tier", "fixer_floor", "fixer_ok", "retired"]

FLASH, STANDARD, STRONG = 0, 1, 2

_FLASH_RE = re.compile(r"flash|mini\b|-mini|nano|lite\b|-lite|haiku|efficient|-free\b|:free|/free\b|small", re.I)
_STRONG_RE = re.compile(r"opus|fable|gpt-6|-sol\b|/sol\b|-pro\b|sonnet", re.I)


def model_tier(model):
    """Tier of *model* (any ``provider/model`` spelling)."""
    name = str(model or "")
    if _FLASH_RE.search(name):
        return FLASH
    if _STRONG_RE.search(name):
        return STRONG
    return STANDARD


def fixer_floor(writer_model=None):
    """Lowest tier a fixer may have: standard, or the writer's tier if higher."""
    return max(STANDARD, model_tier(writer_model) if writer_model else STANDARD)


def fixer_ok(model, writer_model=None):
    return model_tier(model) >= fixer_floor(writer_model)


#: D-2744: model families with no working account; any leg of them is refused.
RETIRED_FAMILIES = ("deepseek",)


def retired(model, family=None):
    """Retired family name for a leg (``family`` or a substring of the id), or None."""
    for name in RETIRED_FAMILIES:
        if str(family or "").lower() == name or name in str(model or "").lower():
            return name
    return None
