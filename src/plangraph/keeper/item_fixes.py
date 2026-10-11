"""MK-FIXES-ERROR: the ``fixes`` item field and the ``error`` evidence record.

``fixes`` is the key of the failed item a new item repairs.  At most ``FIXES_MAX``
live (non-cancelled) items may fix the same target (skill section 5), so a third
open-or-done attempt is refused.  An ``error`` evidence ref is a compressed
error record ``{code <= 64, msg <= 500, run_id?, where?}`` (skill section 6); the
verifier records it as unverified, so it never counts toward done or merged.
"""
from __future__ import annotations

from .credential_guard import guard
from .errors import Refused
from .evidence_types import Result

__all__ = ["FIXES_MAX", "check_fix_limit", "check_error_ref", "ErrorRecordVerifier"]

FIXES_MAX = 2
_CAPS = (("code", 64, True), ("msg", 500, True), ("run_id", 128, False),
         ("where", 200, False))
_ALLOWED = frozenset(name for name, _, _ in _CAPS)


def check_fix_limit(items, affected):
    """Refuse an *affected* item whose ``fixes`` target is unknown, itself, or full.

    *items* is the whole resulting item list (cancelled items do not count).
    """
    index = {it["key"] for it in items}
    live = {}
    for it in items:
        if it.get("fixes") and it.get("state") != "cancelled":
            live.setdefault(it["fixes"], []).append(it["key"])
    for it in items:
        if it["key"] not in affected or not it.get("fixes"):
            continue
        target = it["fixes"]
        if target == it["key"]:
            raise Refused("validation", f"{it['key']}: fixes cannot name itself")
        if target not in index:
            raise Refused("validation", f"{it['key']}: fixes unknown key {target}")
        if len(live.get(target, [])) > FIXES_MAX:
            raise Refused("validation",
                          f"{target} already has {FIXES_MAX} fix items"
                          f" (max {FIXES_MAX}); escalate instead")


def check_error_ref(ref, key=None):
    """Return the normalised error record or refuse ``validation`` naming the field."""
    if not isinstance(ref, dict):
        raise Refused("validation", "error evidence ref must be a mapping")
    unknown = sorted(set(ref) - _ALLOWED)
    if unknown:
        raise Refused("validation", f"error evidence has unknown fields {unknown}")
    out = {}
    for name, cap, required in _CAPS:
        value = ref.get(name)
        if value is None:
            if required:
                raise Refused("validation", f"error evidence needs {name}")
            continue
        if not isinstance(value, str) or (required and not value):
            raise Refused("validation", f"error evidence {name} must be a string")
        if len(value) > cap:
            raise Refused("validation", f"error evidence {name} exceeds {cap} characters")
        guard(f"error evidence {name}", value, key)
        out[name] = value
    return out


class ErrorRecordVerifier:
    """Registry entry for ``error``: always unverified, by design."""

    kind = "error"

    def verify(self, item, ref, ctx):
        return Result(False, "error record (never counts as proof)", dict(ref or {}))
