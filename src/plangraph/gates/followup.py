"""§0.1 ONE TASK LOOPS: refuse follow-up items; fixes go into the item's fix log.

A follow-up is an item whose id ends in a fix-round suffix (``-LOWS``, ``-FIX``,
``-FU``, ``-FOLLOWUP``, ``-SQ<n>``, ``-R<n>``, optionally numbered) or whose
``fixes``/``corrects`` names an item that is not finished yet.  Only the
router (the operator's channel) and the ``system`` role (planner) may create
one; everyone else logs the fix on the existing item (``log_item``).
"""
from __future__ import annotations

import re

__all__ = ["FOLLOWUP_SUFFIX", "EXEMPT_ROLES", "followup_reason", "check_new_items"]

FOLLOWUP_SUFFIX = re.compile(
    r"-(?:LOWS?|FIX(?:ES)?|FU|FOLLOW-?UPS?)(?:-?\d+)?$|-(?:SQ|R)-?\d+$", re.I)
EXEMPT_ROLES = frozenset({"router", "system"})
_FINISHED = frozenset({"done", "cancelled"})


def _qualified(project, ref):
    return ref if ":" in ref else f"{project}:{ref}"


def followup_reason(definition, items_by_key):
    """Why *definition* is a follow-up item, or ``None``.

    *definition* carries ``project``, ``id`` (or ``key``), and optional
    ``fixes``/``corrects``; *items_by_key* maps existing keys to items.
    """
    ident = definition.get("id") or str(definition.get("key", "")).split(":", 1)[-1]
    match = FOLLOWUP_SUFFIX.search(ident or "")
    if match:
        return f"id suffix {match.group(0)!r} marks a follow-up item"
    project = definition.get("project") or ""
    for field in ("fixes", "corrects"):
        ref = definition.get(field)
        if not isinstance(ref, str) or not ref:
            continue
        target = items_by_key.get(_qualified(project, ref))
        if target is not None and target.get("state") not in _FINISHED:
            return f"{field} names {target['key']}, which is still {target.get('state')}"
    return None


def check_new_items(role, definitions, items_by_key):
    """Return ``(key, reason)`` of the first refused follow-up, else ``None``."""
    if role in EXEMPT_ROLES:
        return None
    for definition in definitions:
        reason = followup_reason(definition, items_by_key)
        if reason:
            return definition.get("key") or definition.get("id"), reason
    return None
