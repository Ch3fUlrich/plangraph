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


_ENTRY = re.compile(r"([A-Za-z_][\w.-]*): ")


def split_legacy_metadata(note):
    """``(authored note, {name: value})`` for a note an older Keeper folded metadata into.

    The older fold appended, in sorted name order, one ``name: value`` entry per unknown field
    (a multi-line value runs on over further lines).  The entries at the end of the note whose
    names ascend are that fold; the ``METADATA_FIELDS`` among them are metadata and are
    returned (a list or mapping value parsed), every other entry stays in the note.  Only a
    note whose creation event this Keeper did not write with the note may be read this way: the
    caller knows that from the event, the text alone cannot tell the fold from an author.
    """
    if not isinstance(note, str):
        return note, {}
    lines = note.split("\n")
    starts = [i for i, line in enumerate(lines) if _ENTRY.match(line)]
    first = len(starts)
    while first > 1 and (_ENTRY.match(lines[starts[first - 2]]).group(1)
                         < _ENTRY.match(lines[starts[first - 1]]).group(1)):
        first -= 1
    kept, found = lines[:starts[first - 1]] if starts else lines, {}
    for at, start in enumerate(starts[first - 1:] if starts else []):
        end = starts[first + at] if first + at < len(starts) else len(lines)
        name = _ENTRY.match(lines[start]).group(1)
        if name not in METADATA_FIELDS:
            kept.extend(lines[start:end])
            continue
        text = "\n".join([lines[start][_ENTRY.match(lines[start]).end():], *lines[start + 1:end]])
        if text[:1] in "[{":
            try:
                text = json.loads(text)
            except ValueError:
                pass
        found.setdefault(name, text)
    return ("\n".join(kept) if kept else None), found


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
