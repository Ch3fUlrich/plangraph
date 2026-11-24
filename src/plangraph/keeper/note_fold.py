"""Fold unknown item fields into ``note`` (shared by the importer and create).

The YAML importer has always kept an item's extra fields (``intent``,
``workstreams``, ``budget`` ...) by appending ``name: value`` lines to its
``note``.  B3 makes the API/batch create path do the same, through this
one helper, so the same item gives the same note whichever door it came by.
"""
from __future__ import annotations

import json
import re

from .errors import Refused

NOTE_CAP = 2000
#: ``description`` is a real column (MODEL-NAMES), no longer folded; same cap.
DESCRIPTION_CAP = 2000


#: Item metadata that is not premise text.  A create or import that does not know the field
#: folds it into ``note`` as a ``name: value`` line (:func:`fold_note`); the decision gate must
#: not read an id from such a line, whichever door folded it.
METADATA_FIELDS = ("session", "similar", "category", "supersedes")
_METADATA_LINE = re.compile(r"(?m)^(?:" + "|".join(METADATA_FIELDS) + r"): .*(?:\n|\Z)")


def premise_note(note):
    """*note* without the folded metadata lines: the text a decision may be cited in."""
    return _METADATA_LINE.sub("", note).rstrip("\n") if isinstance(note, str) else note


def render(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def fold_note(base, extra, key):
    """``base`` note followed by one ``name: value`` line per extra field."""
    parts = []
    if base is not None:
        if not isinstance(base, str):
            raise Refused("validation", f"{key}: note must be a string")
        if base:
            parts.append(base)
    parts.extend(f"{name}: {render(extra[name])}" for name in sorted(extra))
    return "\n".join(parts) if parts else None


def fold_checked(base, extra, key):
    """``fold_note`` that refuses (never truncates) a note over ``NOTE_CAP``."""
    note = fold_note(base, extra, key)
    if note is not None and extra and len(note) > NOTE_CAP:
        names = ", ".join(sorted(extra))
        raise Refused("validation",
                      f"{key}: note exceeds {NOTE_CAP} characters once the "
                      f"unknown field(s) {names} are folded into it")
    return note
