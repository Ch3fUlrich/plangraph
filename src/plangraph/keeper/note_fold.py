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


#: Item metadata that is not premise text: create and import validate these fields and carry them
#: on the creation event; they are never folded into ``note`` (which a decision may be cited in).
METADATA_FIELDS = ("session", "similar", "category", "supersedes")


_LEGACY_LINE = re.compile(r"(%s): (.*)" % "|".join(METADATA_FIELDS))


def split_legacy_metadata(note, whole=False):
    """``(authored note, {name: value})`` for a note an older Keeper folded metadata into.

    The older fold appended one ``name: value`` line per metadata field to the end of the
    note; those trailing lines are metadata, not authored text.  A value that was a list or a
    mapping comes back parsed.  A note that is nothing but such lines is returned whole unless
    *whole* says the caller knows an older Keeper wrote it (from the creation event): it is
    indistinguishable from one an author wrote.
    """
    if not isinstance(note, str):
        return note, {}
    lines, found = note.split("\n"), {}
    while lines and (match := _LEGACY_LINE.fullmatch(lines[-1])):
        text = match.group(2)
        if text[:1] in "[{":
            try:
                text = json.loads(text)
            except ValueError:
                pass
        found.setdefault(match.group(1), text)
        lines.pop()
    if not found or not (lines or whole):
        return note, {}
    return ("\n".join(lines) if lines else None), found


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
