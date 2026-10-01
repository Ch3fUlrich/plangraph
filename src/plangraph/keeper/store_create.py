"""Definition verbs for :class:`plangraph.keeper.store.Store`.

These are the verbs that create and edit the graph itself: ``create_items``,
``import_items``, ``edit_definition`` and ``set_priority``.  They only touch the
definition columns plus the ``edges`` table; the lifecycle transitions live in
``store_verbs`` and the pure helpers on the ``Store`` base class.
"""
from __future__ import annotations

import json

from .errors import Refused

_EDITABLE = ("title", "estimate_h", "done_criteria", "evidence_kinds",
             "depends_on", "track", "owner", "note")
_MAX_BATCH = 500
_MAX_DEPS = 200
_ITEM_COLUMNS = ("key", "project", "id", "title", "kind", "track", "owner",
                 "owner_family",
                 "estimate_h", "estimate_src", "done_criteria", "evidence_kinds",
                 "operator", "priority", "source", "note", "corrects", "state",
                 "epoch", "claimed_by", "delegate", "lease_until", "reviewer",
                 "model", "created_at", "updated_at")


class CreateMixin:
    """Create, import and edit item definitions."""

    # -------------------------------------------------------- create/import
    def create_items(self, principal, items):
        """Router (any project) or L1 (own project): create definitions."""
        return self._run(principal, "create_items", None,
                         lambda now: self._create(principal, items, now, False))

    def import_items(self, principal, items):
        """Router only: create legacy rows whose ``state`` is honoured."""
        return self._run(principal, "import_items", None,
                         lambda now: self._create(principal, items, now, True))

    def _create(self, principal, items, now, legacy):
        if len(items) > _MAX_BATCH:
            raise Refused("validation", f"batch exceeds {_MAX_BATCH} items")
        defs = [self._normalise(raw) for raw in items]
        for definition in defs:
            self._may_manage(principal, definition["project"])
        if principal.role != "router":
            for definition in defs:
                for dep in definition["depends_on"]:
                    if not dep.startswith(definition["project"] + ":"):
                        raise Refused("forbidden",
                                      "cross-project dependency needs the router")
        if legacy:
            self._require_role(principal, "router")
            for definition in defs:
                if definition["state"] not in ("open", "claimed", "in_review"):
                    raise Refused("validation",
                                  "import only honours open/claimed/in_review")
        else:
            for definition in defs:
                definition["state"] = "open"
        keys = [d["key"] for d in defs]
        if len(set(keys)) != len(keys):
            raise Refused("validation", "duplicate key in batch")
        for definition in defs:
            existing = self._item(definition["key"])
            if existing is not None:
                self._assert_unlocked(existing)
                raise Refused("conflict", f"{definition['key']} already exists")
        before = self._all_items()
        self._validate_change(before + defs, before, affected=keys)
        with self._write():
            for definition in defs:
                self._insert_item(principal, definition, now)
        return {"created": keys}

    def _insert_item(self, principal, definition, now):
        state = definition["state"]
        epoch = 1 if state in ("claimed", "in_review") else 0
        row = {
            "key": definition["key"], "project": definition["project"],
            "id": definition["id"], "title": definition.get("title", ""),
            "kind": definition.get("kind", "lane"), "track": definition.get("track"),
            "owner": definition.get("owner"),
            "owner_family": definition.get("owner_family"),
            "estimate_h": definition.get("estimate_h"),
            "estimate_src": definition.get("estimate_src"),
            "done_criteria": definition.get("done_criteria"),
            "evidence_kinds": json.dumps(definition.get("evidence_kinds") or ["any"]),
            "operator": 1 if definition.get("operator") else 0,
            "priority": int(definition.get("priority") or 0),
            "source": definition.get("source"), "note": definition.get("note"),
            "corrects": definition.get("corrects"), "state": state, "epoch": epoch,
            "claimed_by": (definition.get("owner")
                           if state in ("claimed", "in_review") else None),
            "delegate": None, "lease_until": None,
            "reviewer": None, "model": None, "created_at": self._iso(now),
            "updated_at": self._iso(now),
        }
        placeholders = ",".join("?" for _ in _ITEM_COLUMNS)
        self.conn.execute(
            f"INSERT INTO items({','.join(_ITEM_COLUMNS)}) VALUES({placeholders})",
            tuple(row[column] for column in _ITEM_COLUMNS))
        for dep in definition.get("depends_on") or []:
            self.conn.execute(
                "INSERT INTO edges(src,dst,rel) VALUES(?,?, 'depends_on')",
                (definition["key"], dep))
        self._append(principal, definition["key"], "item_created",
                     {"state": "open", "priority": row["priority"]}, now)
        if state in ("claimed", "in_review"):
            self._append(principal, definition["key"], "claimed",
                         {"epoch": 1, "claimed_by": definition.get("owner"),
                          "legacy": True}, now)
        if state == "in_review":
            self._append(principal, definition["key"], "submitted",
                         {"legacy": True}, now)

    # ------------------------------------------------------ edit/priority
    def edit_definition(self, principal, key, fields):
        return self._run(principal, "edit_definition", key,
                         lambda now: self._edit(principal, key, fields, now))

    def _edit(self, principal, key, fields, now):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        self._may_manage(principal, item["project"])
        if item["state"] != "open":
            raise Refused("bad_state", f"{key} is {item['state']}, not open")
        if not isinstance(fields, dict):
            raise Refused("validation", "edit body must be a mapping")
        unknown = set(fields) - set(_EDITABLE)
        if unknown:
            raise Refused("validation", f"not editable: {sorted(unknown)}")
        self._check_text(fields)
        before = self._all_items()
        candidate = self._all_items()
        me = next(it for it in candidate if it["key"] == key)
        for field, value in fields.items():
            if field == "depends_on":
                if not isinstance(value, (list, tuple)):
                    raise Refused("validation", "depends_on must be a list")
                if len(value) > _MAX_DEPS:
                    raise Refused("validation",
                                  f"depends_on exceeds {_MAX_DEPS} entries")
                if any(not isinstance(d, str) for d in value):
                    raise Refused("validation",
                                  "depends_on entries must be strings")
                deps = [self._qualify(item["project"], d) for d in value]
                if any(not d.startswith(item["project"] + ":") for d in deps) \
                        and principal.role != "router":
                    raise Refused("forbidden",
                                  "cross-project dependency needs the router")
                me["depends_on"] = deps
            else:
                me[field] = value
        self._validate_change(candidate, before, affected={key})
        assignments = [field for field in fields if field != "depends_on"]
        with self._write():
            # Re-read inside the transaction: a competitor may have claimed or
            # otherwise moved the item between the read above and here.
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != "open" or fresh["epoch"] != item["epoch"]:
                raise Refused("conflict",
                              f"{key} changed under the transaction")
            self._may_manage(principal, fresh["project"])
            if assignments:
                sets = ", ".join(f"{field}=?" for field in assignments)
                values = [self._stored(field, fields[field]) for field in assignments]
                cursor = self.conn.execute(
                    f"UPDATE items SET {sets}, updated_at=?"
                    " WHERE key=? AND state='open' AND epoch=?",
                    (*values, self._iso(now), key, item["epoch"]))
                self._race_guard(cursor, key, "open", item["epoch"])
            else:
                self._guarded_touch(key, ("open",), item["epoch"])
            if "depends_on" in fields:
                self.conn.execute("DELETE FROM edges WHERE src=?", (key,))
                for dep in me["depends_on"]:
                    self.conn.execute(
                        "INSERT INTO edges(src,dst,rel) VALUES(?,?, 'depends_on')",
                        (key, dep))
            self._append(principal, key, "item_defined",
                         {"fields": sorted(fields)}, now)
        return {"key": key}

    def set_priority(self, principal, key, n):
        return self._run(principal, "set_priority", key,
                         lambda now: self._priority(principal, key, n, now))

    def _priority(self, principal, key, n, now):
        self._require_role(principal, "router")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        with self._write():
            self.conn.execute("UPDATE items SET priority=?, updated_at=? WHERE key=?",
                              (int(n), self._iso(now), key))
            self._append(principal, key, "priority_set", {"priority": int(n)}, now)
        return {"key": key, "priority": int(n)}
