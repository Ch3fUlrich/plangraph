"""PG-SET-KIND: the router turns an open ``lane`` into a ``question`` / ``operator_task``.

``set_kind(key, kind)`` is the one way to change an item's kind after creation
(``edit_definition`` cannot): a planned lane that turns out to wait on the
operator.  One hash-chained ``kind_set`` event ``{kind, was, operator, wait}``;
the cache row gets ``kind``, ``operator = 1`` and the kind's default ``wait``
(an explicit ``wait`` is kept), exactly what ``create_items`` would have stored
for that kind.  ``events.fold`` replays ``kind`` and ``wait`` from the event, so
``verify_integrity`` agrees with the cache.

Guards: router only; target kind ``question`` or ``operator_task`` only; the
item must be ``open`` and a ``lane`` today (no claim, review or result is ever
re-labelled, and the kind cannot flip back and forth).
"""
from __future__ import annotations

from .errors import Refused
from .item_fields import KIND_DEFAULTS

__all__ = ["KindMixin", "SETTABLE_KINDS"]

#: The only kinds ``set_kind`` may move a lane to.
SETTABLE_KINDS = ("question", "operator_task")


class KindMixin:
    """Router-only ``set_kind``."""

    def set_kind(self, principal, key, kind):
        return self._run(principal, "set_kind", key,
                         lambda now: self._set_kind(principal, key, kind, now))

    def _set_kind(self, principal, key, kind, now):
        self._require_role(principal, "router")
        if kind not in SETTABLE_KINDS:
            raise Refused("validation", f"kind must be one of {list(SETTABLE_KINDS)}")
        # Read, assert and write inside one transaction: the item may have been
        # claimed or closed before the write lock, and then gets no event.
        with self._write():
            item = self._get_or_refuse(key)
            self._assert_unlocked(item)
            if item["state"] != "open":
                raise Refused("bad_state", f"{key} is {item['state']}, not open")
            if item["kind"] != "lane":
                raise Refused("validation",
                              f"{key} is a {item['kind']}; only a lane can change kind")
            wait = item["wait"] if item["wait"] is not None else KIND_DEFAULTS[kind][1]
            cursor = self.conn.execute(
                "UPDATE items SET kind=?, operator=1, wait=?, updated_at=?"
                " WHERE key=? AND state='open' AND kind='lane' AND epoch=?",
                (kind, wait, self._iso(now), key, item["epoch"]))
            self._row_guard(cursor, key, "open", item["epoch"])
            self._append(principal, key, "kind_set",
                         {"kind": kind, "was": "lane", "operator": True, "wait": wait}, now)
        return {"key": key, "kind": kind, "was": "lane"}
