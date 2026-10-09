"""Definition verbs for :class:`plangraph.keeper.store.Store`.

These are the verbs that create and edit the graph itself: ``create_items``,
``import_items``, ``edit_definition`` and ``set_priority``.  They only touch the
definition columns plus the ``edges`` table; the lifecycle transitions live in
``store_verbs`` and the pure helpers on the ``Store`` base class.

One state exception (D-434): the router re-tiers claimed/in-review items -- its two tier fields only -- bumping ``round`` so no stale verdict counts.
"""
from __future__ import annotations

import json
import re

from .auth import validate_family
from .errors import Refused
from .credential_guard import guard
from .item_fixes import check_fix_limit
from .item_fields import MODEL_FIELDS, SMALL_FIELDS, check_choices, check_model_fields
from .note_fold import fold_checked
from .store_tiers import (
    REVIEW_TIERS,
    _REVIEW_KEYWORD,
    _TIER_FIELDS,
    default_review_tier,
    tier_needs_router,
    validated_decision_tier,
    validated_review_tier,
    _tier_only,
)

_EDITABLE = ("title", "description", "host", "wait", "layer", "role", "agent_id",
             "phase_kind", "fixes", "choices", "estimate_h", "done_criteria", "evidence_kinds",
             "depends_on", "track", "owner", "note", "review_tier")
_MAX_BATCH = 500
_MAX_DEPS = 200
#: B3: every field ``Store._normalise`` reads; anything else folds into ``note``.
KNOWN_ITEM_FIELDS = frozenset(
    ("project", "id", "key", "title", "description", "host", "wait", "layer", "role",
     "agent_id", "phase_kind", "fixes", "kind", "track", "owner", "owner_family",
     "estimate_h", "estimate_src", "depends_on", "corrects", "done_criteria",
     "evidence_kinds", "operator", "priority", "review_tier", "source", "note",
     "state"))
_ITEM_COLUMNS = ("key", "project", "id", "title", "description", "host", "wait", "layer", "role",
                 "agent_id", "phase_kind", "fixes", "choices", "kind", "track", "owner",
                 "owner_family",
                 "estimate_h", "estimate_src", "done_criteria", "evidence_kinds",
                 "operator", "priority", "review_tier", "source", "note",
                 "corrects", "state",
                 "epoch", "claimed_by", "delegate", "lease_until", "reviewer",
                 "model", "created_at", "updated_at")

def _defined_payload(fields):
    """``item_defined`` payload: the changed field names plus the new tier value.

    The fold replays this to reconstruct ``review_tier``; it needs the value,
    not just the name, so ``verify_integrity`` can agree with the table.
    """
    payload = {"fields": sorted(fields)}
    if "review_tier" in fields:
        payload["review_tier"] = fields["review_tier"]
    for name in SMALL_FIELDS:
        if name in fields:
            payload[name] = fields[name]
    return payload


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

    @staticmethod
    def _folded_note(raw, key):
        """B3: unknown fields fold into ``note`` exactly as the importer does."""
        unknown = {name: raw[name] for name in raw
                   if name not in KNOWN_ITEM_FIELDS}
        for name, value in unknown.items():
            guard(name, value)
        return fold_checked(raw.get("note"), unknown, key) if unknown \
            else raw.get("note")

    def _create(self, principal, items, now, legacy):
        if len(items) > _MAX_BATCH:
            raise Refused("validation", f"batch exceeds {_MAX_BATCH} items")
        defs = [self._normalise(raw) for raw in items]
        for raw in items:
            if isinstance(raw, dict):
                validate_family(raw.get("owner_family"))
        for raw, definition in zip(items, defs):
            definition["review_tier"] = self._tier_for(raw, definition, principal)
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
        check_fix_limit(before + defs, set(keys))
        self._validate_change(before + defs, before, affected=keys)
        with self._write():
            for definition in defs:
                self._insert_item(principal, definition, now)
        result = {"created": keys}
        if any(not definition.get("description") for definition in defs):
            result["warnings"] = ["description missing: brief will be empty"]
        return result

    def _tier_for(self, raw, definition, principal):
        """The tier to store: an explicit (validated) value, else the default.

        An explicit tier LOWER than the computed default requires the router role.
        Raising or keeping the same tier is allowed for router and L1.
        """
        explicit = raw.get("review_tier") if isinstance(raw, dict) else None
        default = default_review_tier(definition["project"], definition["id"],
                                      definition.get("title"), definition.get("track"))
        if explicit is not None:
            tier = validated_review_tier(explicit)
            if tier_needs_router(tier) and principal.role != "router":
                raise Refused("forbidden",
                              "only the router may set review_tier='none'")
            # Check if explicit tier is lower than default
            # Order: none < standard < high (none already handled above)
            tier_order = {"standard": 1, "high": 2}
            if tier != default and tier_order.get(tier, 0) < tier_order.get(default, 0):
                if principal.role != "router":
                    raise Refused("forbidden",
                                  f"explicit review_tier '{tier}' is lower than "
                                  f"default '{default}'; only the router may lower the tier")
            return validated_decision_tier(tier, definition["evidence_kinds"])
        return default

    def _insert_item(self, principal, definition, now):
        state = definition["state"]
        epoch = 1 if state in ("claimed", "in_review") else 0
        row = {
            "key": definition["key"], "project": definition["project"],
            "id": definition["id"], "title": definition.get("title", ""),
            "description": definition.get("description"),
            "host": definition.get("host"),
            "wait": definition.get("wait"),
            **{name: definition.get(name) for name in MODEL_FIELDS},
            "fixes": definition.get("fixes"),
            "choices": definition.get("choices"),
            "kind": definition.get("kind", "lane"), "track": definition.get("track"),
            "owner": definition.get("owner"),
            "owner_family": definition.get("owner_family"),
            "estimate_h": definition.get("estimate_h"),
            "estimate_src": definition.get("estimate_src"),
            "done_criteria": definition.get("done_criteria"),
            "evidence_kinds": json.dumps(definition.get("evidence_kinds") or ["any"]),
            "operator": 1 if definition.get("operator") else 0,
            "priority": int(definition.get("priority") or 0),
            "review_tier": definition["review_tier"],
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
        created = {"state": "open", "priority": row["priority"],
                   "review_tier": row["review_tier"]}
        for name in SMALL_FIELDS:
            if row[name] is not None:
                created[name] = row[name]
        if definition.get("rehomed_from"):
            created["rehomed_from"] = definition["rehomed_from"]
        self._append(principal, definition["key"], "item_created", created, now)
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
        # D-434: the router's one narrow exception -- the two tier fields alone on
        # a claimed/in-review item; anyone else fails on authorisation, not state.
        tier_edit = item["state"] in ("claimed", "in_review") and _tier_only(fields)
        if tier_edit and principal.role != "router":
            raise Refused("forbidden", "only the router may edit review_tier or "
                          "evidence_kinds while an item is claimed or in review")
        if item["state"] != "open" and not tier_edit:
            raise Refused("bad_state", f"{key} is {item['state']}, not open")
        if not isinstance(fields, dict):
            raise Refused("validation", "edit body must be a mapping")
        unknown = set(fields) - set(_EDITABLE)
        if unknown:
            raise Refused("validation", f"not editable: {sorted(unknown)}")
        self._check_text(fields)
        check_model_fields(fields, item["kind"], key)
        if "review_tier" in fields:
            validated_review_tier(fields["review_tier"])
            if (item["review_tier"] == "high"
                    and fields["review_tier"] == "standard"
                    and principal.role != "router"):
                raise Refused("forbidden", "only the router may lower review_tier")
        # X7b: the *resulting* (tier, evidence_kinds) pair must hold, so an edit
        # of either field alone cannot leave a `none` item with looser evidence.
        resulting_tier = fields.get("review_tier", item["review_tier"])
        if tier_needs_router(resulting_tier):
            if (resulting_tier != item["review_tier"]
                    and principal.role != "router"):
                raise Refused("forbidden",
                              "only the router may set review_tier='none'")
            validated_decision_tier(
                resulting_tier,
                fields.get("evidence_kinds", item["evidence_kinds"]))
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
            elif field == "choices":
                value = fields["choices"] = me["choices"] = check_choices(
                    value, item["kind"], key)
            elif field == "fixes" and value is not None:
                if not isinstance(value, str) or not value:
                    raise Refused("validation", "fixes must be a key string or null")
                value = fields["fixes"] = me["fixes"] = self._qualify(
                    item["project"], value)
            else:
                me[field] = value
        if "review_tier" not in fields and me.get("review_tier") == "standard":
            # Renaming into risky territory raises standard->high
            # automatically.  Only an actual title/track rename triggers it;
            # an explicit tier in the same edit (or an already-high/none
            # item) is never touched, so this never lowers.  (``id`` is not
            # editable, so there is nothing to watch there.)
            renamed = [fields[field] for field in ("title", "track")
                       if field in fields]
            if any(isinstance(value, str) and _REVIEW_KEYWORD.search(value)
                   for value in renamed):
                fields["review_tier"] = "high"
                me["review_tier"] = "high"
        check_fix_limit(candidate, {key})
        self._validate_change(candidate, before, affected={key})
        assignments = [field for field in fields if field != "depends_on"]
        with self._write():
            # Re-read inside the transaction: a competitor may have claimed or
            # otherwise moved the item between the read above and here.
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            # ``tier_edit`` implies router; guard the write on the states the read passed.
            states = ("claimed", "in_review") if tier_edit else ("open",)
            if fresh["state"] not in states or fresh["epoch"] != item["epoch"]:
                raise Refused("conflict",
                              f"{key} changed under the transaction")
            self._may_manage(principal, fresh["project"])
            if tier_edit:
                # Re-check the resulting (tier, kinds) pair under the lock:
                # evidence_kinds may have been edited under us since the read.
                validated_decision_tier(
                    fields.get("review_tier", fresh["review_tier"]),
                    fields.get("evidence_kinds", fresh["evidence_kinds"]))
                # Bump the round like ``submit`` does: earlier verdicts stop
                # matching ``_round_verdicts`` -- no stale count under the new tier.
                round_no = int(fresh.get("round") or 0) + 1
            if assignments:
                sets = ", ".join(f"{field}=?" for field in assignments)
                values = [self._stored(field, fields[field]) for field in assignments]
                if tier_edit:
                    sets += ", round=?"
                    values.append(round_no)
                guard = "/".join(states)
                cursor = self.conn.execute(
                    f"UPDATE items SET {sets}, updated_at=?"
                    f" WHERE key=? AND state IN ({','.join('?' for _ in states)})"
                    " AND epoch=?",
                    (*values, self._iso(now), key, *states, item["epoch"]))
                self._race_guard(cursor, key, guard, item["epoch"])
            else:
                self._guarded_touch(key, ("open",), item["epoch"])
            if "depends_on" in fields:
                self.conn.execute("DELETE FROM edges WHERE src=?", (key,))
                for dep in me["depends_on"]:
                    self.conn.execute(
                        "INSERT INTO edges(src,dst,rel) VALUES(?,?, 'depends_on')",
                        (key, dep))
            payload = _defined_payload(fields)
            if tier_edit:
                # D-434 marker: the state the router re-tiered through; the
                # fold reads it to replay the round bump that voids verdicts.
                payload["while"] = fresh["state"]
            self._append(principal, key, "item_defined", payload, now)
        return {"key": key}

    def set_priority(self, principal, key, n):
        return self._run(principal, "set_priority", key,
                         lambda now: self._priority(principal, key, n, now))

    def _priority(self, principal, key, n, now):
        self._require_role(principal, "router")
        # Read and assert inside the transaction: the item may have been
        # cancelled or marked DONE before we take the write lock, and must then
        # get no priority change and no event.  The guarded UPDATE proves the
        # row we read is the one we changed.
        with self._write():
            item = self._get_or_refuse(key)
            self._assert_unlocked(item)
            cursor = self.conn.execute(
                "UPDATE items SET priority=?, updated_at=?"
                " WHERE key=? AND state=? AND epoch=?",
                (int(n), self._iso(now), key, item["state"], item["epoch"]))
            self._row_guard(cursor, key, item["state"], item["epoch"])
            self._append(principal, key, "priority_set", {"priority": int(n)}, now)
        return {"key": key, "priority": int(n)}
