"""MODEL-NAMES router-only ``migrate_descriptions`` verb for :class:`...store.Store`.

Before ``description`` was a column, B3 folded it into ``note`` as a
``description: <text>`` line.  This verb moves that line into the column for
every *active* item, in one transaction:

* router principal only (``forbidden`` otherwise);
* only a note whose FIRST line starts ``description: `` is migrated.  The value
  is that line plus any continuation lines up to the next ``name: `` line (the
  next folded field); the rest of the note stays, or the note becomes NULL;
* an item that already has a description is never overwritten (left as is);
* DONE and cancelled items are locked and never touched;
* one ``item_defined`` event per migrated item, attributed to the caller, whose
  payload carries the new description so ``fold`` and ``verify_integrity``
  agree; a second run finds nothing to do.
"""
from __future__ import annotations

import re

from .note_fold import DESCRIPTION_CAP

_FIELD_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*: ")
_PREFIX = "description: "


def split_folded_description(note):
    """``(description, remaining_note)`` or ``None`` if *note* has no leading line."""
    if not isinstance(note, str) or not note.startswith(_PREFIX):
        return None
    lines = note.split("\n")
    taken = [lines[0][len(_PREFIX):]]
    rest = 1
    while rest < len(lines) and not _FIELD_LINE.match(lines[rest]):
        taken.append(lines[rest])
        rest += 1
    description = "\n".join(taken)
    remaining = "\n".join(lines[rest:])
    return description, (remaining if remaining else None)


class NamesMixin:
    """The router-only ``migrate_descriptions`` verb."""

    def migrate_descriptions(self, principal):
        return self._run(principal, "migrate_descriptions", None,
                         lambda now: self._migrate_descriptions(principal, now))

    def _migrate_descriptions(self, principal, now):
        self._require_role(principal, "router")
        migrated, skipped = [], []
        with self._write():
            for item in self._all_items():
                if item["state"] in ("done", "cancelled"):
                    continue
                parsed = split_folded_description(item.get("note"))
                if parsed is None:
                    continue
                description, remaining = parsed
                if item.get("description"):
                    skipped.append({"key": item["key"],
                                    "reason": "has_description"})
                    continue
                if not description.strip() or len(description) > DESCRIPTION_CAP:
                    skipped.append({"key": item["key"], "reason": "invalid"})
                    continue
                key = item["key"]
                cursor = self.conn.execute(
                    "UPDATE items SET description=?, note=?, updated_at=?"
                    " WHERE key=? AND state=? AND epoch=?",
                    (description, remaining, self._iso(now), key,
                     item["state"], item["epoch"]))
                self._race_guard(cursor, key, item["state"], item["epoch"])
                self._append(principal, key, "item_defined",
                             {"fields": ["description", "note"],
                              "description": description,
                              "migrated": True}, now)
                migrated.append(key)
        return {"migrated": migrated, "skipped": skipped}
