"""MODEL-HOST router-only ``rehome`` verb and the alias table for :class:`...store.Store`.

``rehome(key, new_project, host)`` moves an ``open`` item to another project in
ONE transaction:

* router principal only; the item must be ``open`` (a DONE or cancelled item is
  ``locked`` and never moves; a claimed/in-review item has a lease, epoch and
  evidence that cannot follow it, so it is ``bad_state``);
* the new key ``new_project:<id>`` is created (an ``item_created`` event that
  names ``rehomed_from``, so ``fold`` and ``verify_integrity`` rebuild it) with
  the old item's definition, its dependencies and ``host`` (kept when ``host`` is
  ``None``);
* every unlocked dependent edge is re-pointed to the new key (one
  ``item_defined`` event each).  A locked (DONE/cancelled) dependent keeps its
  edge to the old key: its row cannot change, and the alias resolves it;
* the old item is cancelled with reason ``rehomed to <new key>``;
* an alias ``old -> new`` is recorded (append-only table).

Aliases resolve wherever a key is accepted (``resolve_key``): the API, ``apply_ops``
ops, dependency lists and the ``Closes:``/``Waives:`` tokens of decisions.
"""
from __future__ import annotations

from .errors import Refused
from .item_fields import check_host

_CHAIN = 16


class RehomeMixin:
    """``rehome``, ``aliases`` and ``resolve_key``."""

    # ------------------------------------------------------------ aliases
    def resolve_key(self, key):
        """Follow ``old -> new`` aliases to the live key (``key`` if none)."""
        if not isinstance(key, str):
            return key
        for _ in range(_CHAIN):
            row = self.conn.execute(
                "SELECT new_key FROM aliases WHERE old_key=?", (key,)).fetchone()
            if row is None:
                return key
            key = row[0]
        return key

    def alias_sources(self, key):
        """Every old key that resolves to *key* (sorted)."""
        found, frontier = set(), {key}
        while frontier:
            marks = ",".join("?" for _ in frontier)
            rows = self.conn.execute(
                f"SELECT old_key FROM aliases WHERE new_key IN ({marks})",
                tuple(frontier)).fetchall()
            frontier = {r[0] for r in rows} - found - {key}
            found |= frontier
        return sorted(found)

    def aliases(self, principal):
        return self._run(principal, "aliases", None, lambda now: self._aliases())

    def _aliases(self):
        return [{"old": r["old_key"], "new": r["new_key"], "ts": r["ts"],
                 "actor": r["actor"], "reason": r["reason"]}
                for r in self.conn.execute(
                    "SELECT old_key,new_key,ts,actor,reason FROM aliases"
                    " ORDER BY old_key")]

    # ------------------------------------------------------------- rehome
    def rehome(self, principal, key, new_project, host=None):
        return self._run(principal, "rehome", key,
                         lambda now: self._rehome(principal, key, new_project,
                                                  host, now))

    def _rehome(self, principal, key, new_project, host, now):
        self._require_role(principal, "router")
        if (not isinstance(new_project, str) or not new_project
                or ":" in new_project or new_project != new_project.strip()):
            raise Refused("validation",
                          "new_project must be a project name without ':'")
        check_host(host)
        key = self.resolve_key(key)
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "open":
            raise Refused("bad_state", f"{key} is {item['state']}, not open")
        if item["project"] == new_project:
            raise Refused("validation", f"{key} is already in {new_project}")
        new_key = f"{new_project}:{item['id']}"
        if self._item(new_key) is not None or self.resolve_key(new_key) != new_key:
            raise Refused("conflict", f"{new_key} already exists")
        definition = dict(item, key=new_key, project=new_project, state="open",
                          host=item["host"] if host is None else host,
                          rehomed_from=key)
        before = self._all_items()
        locked = {it["key"] for it in before
                  if it["state"] in ("done", "cancelled")}
        movers = sorted(it["key"] for it in before
                        if key in it["depends_on"] and it["key"] not in locked)
        after = []
        for it in before:
            copy = dict(it)
            if it["key"] == key:
                copy["state"] = "cancelled"
            elif it["key"] in movers:
                copy["depends_on"] = [new_key if d == key else d
                                      for d in it["depends_on"]]
            after.append(copy)
        after.append(dict(definition, depends_on=list(item["depends_on"])))
        # Only locked dependents keep the old (now cancelled) key.
        self._validate_change(after, before, affected={key, new_key, *movers},
                              allow_new={"depends_on_cancelled"})
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != "open" or fresh["epoch"] != item["epoch"]:
                raise Refused("conflict", f"{key} changed under the transaction")
            self._insert_item(principal, definition, now)
            for dependent in movers:
                self.conn.execute("UPDATE edges SET dst=? WHERE src=? AND dst=?",
                                  (new_key, dependent, key))
                self._append(principal, dependent, "item_defined",
                             {"fields": ["depends_on"],
                              "rehomed": {key: new_key}}, now)
            reason = f"rehomed to {new_key}"
            self._append(principal, key, "cancelled",
                         {"reason": reason, "rehomed_to": new_key}, now)
            cursor = self.conn.execute(
                "UPDATE items SET state='cancelled', lease_until=NULL,"
                " review_waiver=NULL, updated_at=? WHERE key=? AND state='open'"
                " AND epoch=?", (self._iso(now), key, fresh["epoch"]))
            self._row_guard(cursor, key, "open", fresh["epoch"])
            self.conn.execute(
                "INSERT INTO aliases(old_key,new_key,ts,actor,reason)"
                " VALUES(?,?,?,?,?)",
                (key, new_key, self._iso(now), principal.name, reason))
        return {"old": key, "new": new_key, "host": definition["host"],
                "repointed": movers}
