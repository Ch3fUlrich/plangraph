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
from .item_fields import MODEL_FIELDS, SMALL_FIELDS, check_choices, check_kg_refs, check_model_fields
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
             "phase_kind", "fixes", "choices", "kg_refs", "estimate_h", "estimate_src",
             "done_criteria", "evidence_kinds",
             "depends_on", "track", "owner", "note", "review_tier")
_MAX_BATCH = 500
_MAX_DEPS = 200
#: B3: every field ``Store._normalise`` reads; anything else folds into ``note``.
KNOWN_ITEM_FIELDS = frozenset(
    ("project", "id", "key", "title", "description", "host", "wait", "layer", "role",
     "agent_id", "phase_kind", "fixes", "choices", "kg_refs", "kind", "track", "owner", "owner_family",
     "estimate_h", "estimate_src", "depends_on", "under", "corrects", "done_criteria",
     "evidence_kinds", "operator", "priority", "review_tier", "source", "note",
     "state"))
_ITEM_COLUMNS = ("key", "project", "id", "title", "description", "host", "wait", "layer", "role",
                 "agent_id", "phase_kind", "fixes", "choices", "kg_refs", "kind", "track", "owner",
                 "owner_family",
                 "estimate_h", "estimate_src", "done_criteria", "evidence_kinds",
                 "operator", "priority", "review_tier", "source", "note",
                 "corrects", "state",
                 "epoch", "claimed_by", "delegate", "lease_until", "reviewer",
                 "model", "created_at", "updated_at")

def _defined_payload(fields):
    """``item_defined`` payload: the changed field names plus the new tier value.

    The fold replays this to reconstruct ``review_tier``; it needs the value,
    not just the name, so ``verify_integrity`` can agree with the table.  The
    estimate pair rides along for the same reason: an event must say what the
    edit actually stored.  ``fold`` ignores names it does not compare
    (``estimate_h``/``estimate_src`` are definition columns, not cached ones),
    so carrying them keeps the event honest without touching the replay.
    """
    payload = {"fields": sorted(fields)}
    if "review_tier" in fields:
        payload["review_tier"] = fields["review_tier"]
    for name in SMALL_FIELDS:
        if name in fields:
            payload[name] = fields[name]
    for name in ("estimate_h", "estimate_src"):
        if name in fields:
            payload[name] = fields[name]
    return payload


class CreateMixin:
    """Create, import and edit item definitions."""

    # -------------------------------------------------------- create/import
    def create_items(self, principal, items):
        """Router (any project) or L1 (own project): create definitions.

        An optional per-item ``under: <key>`` also writes the gate -> new edge
        (the gate ``depends_on`` the new item) in the same transaction, so the
        new item reaches the goal without a router edit afterwards.
        """
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
            guard(name, value, key)
        return fold_checked(raw.get("note"), unknown, key) if unknown \
            else raw.get("note")

    def _create(self, principal, items, now, legacy):
        if len(items) > _MAX_BATCH:
            raise Refused("validation", f"batch exceeds {_MAX_BATCH} items")
        if legacy and any(isinstance(raw, dict) and raw.get("under") is not None
                          for raw in items):
            # ``under`` is a create-only attachment; a legacy import may not
            # smuggle one in (checked before _normalise resolves it).
            raise Refused("validation", "import_items does not accept 'under'")
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
        # ``under``: resolve and authorise every gate before anything is
        # written.  A gate must already exist -- a key created earlier in this
        # very batch does not exist yet and is refused as not_found.
        attach = []
        for definition in defs:
            gate = self._under_gate(principal, definition)
            if gate is not None:
                attach.append((definition, gate))
        before = self._all_items()
        check_fix_limit(before + defs, set(keys))
        # Validate the graph the writes would leave behind: every gate gains the
        # new item as a dependency, so cycles and reachability are caught here,
        # before a single row is inserted.
        candidate = [dict(item) for item in before] + defs
        affected = set(keys)
        if attach:
            linked = {item["key"]: item for item in candidate}
            for definition, gate in attach:
                gate_item = linked[gate["key"]]
                gate_item["depends_on"] = sorted(
                    set(gate_item.get("depends_on") or [])
                    | {definition["key"]})
                affected.add(gate["key"])
        self._validate_change(candidate, before, affected=affected)
        with self._write():
            for definition in defs:
                self._insert_item(principal, definition, now)
            for definition, gate in attach:
                # Re-read the gate under the lock: it may have been done or
                # cancelled between the read above and here.  The guarded
                # UPDATE both proves the row is the one we read and touches its
                # updated_at, then the gate -> new edge goes in beside it.
                cursor = self.conn.execute(
                    "UPDATE items SET updated_at=? WHERE key=? AND state=?"
                    " AND epoch=?",
                    (self._iso(now), gate["key"], gate["state"], gate["epoch"]))
                self._row_guard(cursor, gate["key"], gate["state"],
                                gate["epoch"])
                self.conn.execute(
                    "INSERT INTO edges(src,dst,rel) VALUES(?,?, 'depends_on')",
                    (gate["key"], definition["key"]))
                self._append(principal, gate["key"], "item_defined",
                             {"fields": ["depends_on"]}, now)
        result = {"created": keys}
        if attach:
            result["attached"] = [{"key": definition["key"],
                                   "under": gate["key"]}
                                  for definition, gate in attach]
        if any(not definition.get("description") for definition in defs):
            result["warnings"] = ["description missing: brief will be empty"]
        return result

    def _under_gate(self, principal, definition):
        """The gate ``definition['under']`` names, or ``None`` for no attach.

        The key was qualified by ``_normalise`` (a bare id means the new item's
        project, then the alias table).  The gate must exist, the caller must
        manage its project, a non-router may only attach inside the new item's
        own project, and a DONE/cancelled gate is ``bad_state`` (locked gates
        are refused by ``_assert_unlocked``).
        """
        gate_key = definition.get("under")
        if not gate_key:
            return None
        gate = self._get_or_refuse(gate_key)
        self._may_manage(principal, gate["project"])
        if principal.role != "router" and gate["project"] != definition["project"]:
            raise Refused("forbidden",
                          "cross-project 'under' needs the router")
        if gate["state"] in ("done", "cancelled"):
            raise Refused("bad_state", f"{gate['key']} is {gate['state']}")
        self._assert_unlocked(gate)
        return gate

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
            "kg_refs": definition.get("kg_refs"),
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
        has_priority = isinstance(fields, dict) and "priority" in fields
        prio = fields["priority"] if has_priority else None
        rest = ({name: value for name, value in fields.items()
                 if name != "priority"} if has_priority else fields)
        if has_priority:
            # Same order as ``_priority``: role, value, then the item lookup.
            self._require_role(principal, "router")
            self._checked_priority(prio)
            if not rest:
                with self._write():
                    self._apply_priority(principal, key, prio, now)
                return {"key": key, "priority": int(prio)}
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        self._may_manage(principal, item["project"])
        # D-434: the router's one narrow exception -- the two tier fields alone on
        # a claimed/in-review item; anyone else fails on authorisation, not state.
        tier_edit = item["state"] in ("claimed", "in_review") and _tier_only(rest)
        if tier_edit and principal.role != "router":
            raise Refused("forbidden", "only the router may edit review_tier or "
                          "evidence_kinds while an item is claimed or in review")
        if item["state"] != "open" and not tier_edit:
            raise Refused("bad_state", f"{key} is {item['state']}, not open")
        if not isinstance(rest, dict):
            raise Refused("validation", "edit body must be a mapping")
        unknown = set(rest) - set(_EDITABLE)
        if unknown:
            raise Refused("validation", f"not editable: {sorted(unknown)}")
        edit_fields = dict(rest)
        self._check_text(edit_fields, key)        # caps + credential guard (incl. estimate_src)
        # PG-ESTIMATE-SRC-EDIT: an estimate needs a stated source, otherwise the
        # audit-meta ``default`` marker keeps flagging the item forever.  An
        # ``estimate_h`` edit that does not say where the number came from
        # attributes it to the editing principal and records the auto-fill.
        auto_src = False
        effective_h = edit_fields.get("estimate_h", item.get("estimate_h"))
        if "estimate_h" in edit_fields and effective_h is None:
            # Clearing the estimate clears its source; a source cannot outlive it.
            if edit_fields.get("estimate_src") is not None:
                raise Refused("validation", "estimate_src needs an estimate_h")
            edit_fields["estimate_src"] = None
        elif "estimate_h" in edit_fields and "estimate_src" not in edit_fields:
            edit_fields["estimate_src"] = principal.name
            auto_src = True
        if edit_fields.get("estimate_src") is not None or (
                "estimate_src" in edit_fields and effective_h is not None):
            src = edit_fields["estimate_src"]
            if not isinstance(src, str) or not src.strip():
                raise Refused("validation", "estimate_src must be a non-empty string")
            if effective_h is None:
                raise Refused("validation", "estimate_src needs an estimate_h")
        check_model_fields(edit_fields, item["kind"], key)
        if "review_tier" in edit_fields:
            validated_review_tier(edit_fields["review_tier"])
            if (item["review_tier"] == "high"
                    and edit_fields["review_tier"] == "standard"
                    and principal.role != "router"):
                raise Refused("forbidden", "only the router may lower review_tier")
        # X7b: the *resulting* (tier, evidence_kinds) pair must hold, so an edit
        # of either field alone cannot leave a `none` item with looser evidence.
        resulting_tier = edit_fields.get("review_tier", item["review_tier"])
        if tier_needs_router(resulting_tier):
            if (resulting_tier != item["review_tier"]
                    and principal.role != "router"):
                raise Refused("forbidden",
                              "only the router may set review_tier='none'")
            validated_decision_tier(
                resulting_tier,
                edit_fields.get("evidence_kinds", item["evidence_kinds"]))
        before = self._all_items()
        candidate = self._all_items()
        me = next(it for it in candidate if it["key"] == key)
        by_key = {it["key"]: it for it in candidate}
        dropped = []
        for field, value in edit_fields.items():
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
                # A dependency on an item that is cancelled is dropped from the
                # resulting list instead of refusing the edit: the rest of the
                # list stands, the caller is told what went away, and the graph
                # validates (views.validate would report depends_on_cancelled).
                # An unknown key is kept here and refused by _validate_change.
                kept = []
                for dep in deps:
                    target = by_key.get(dep)
                    if target is not None and target["state"] == "cancelled":
                        dropped.append(f"dropped cancelled dependency {dep}")
                        continue
                    kept.append(dep)
                if any(not d.startswith(item["project"] + ":") for d in kept) \
                        and principal.role != "router":
                    raise Refused("forbidden",
                                  "cross-project dependency needs the router")
                me["depends_on"] = kept
            elif field == "kg_refs":
                value = edit_fields["kg_refs"] = me["kg_refs"] = check_kg_refs(value, key)
            elif field == "choices":
                value = edit_fields["choices"] = me["choices"] = check_choices(
                    value, item["kind"], key)
            elif field == "fixes" and value is not None:
                if not isinstance(value, str) or not value:
                    raise Refused("validation", "fixes must be a key string or null")
                value = edit_fields["fixes"] = me["fixes"] = self._qualify(
                    item["project"], value)
            else:
                me[field] = value
        if "review_tier" not in edit_fields and me.get("review_tier") == "standard":
            # Renaming into risky territory raises standard->high
            # automatically.  Only an actual title/track rename triggers it;
            # an explicit tier in the same edit (or an already-high/none
            # item) is never touched, so this never lowers.  (``id`` is not
            # editable, so there is nothing to watch there.)
            renamed = [edit_fields[field] for field in ("title", "track")
                       if field in edit_fields]
            if any(isinstance(value, str) and _REVIEW_KEYWORD.search(value)
                   for value in renamed):
                edit_fields["review_tier"] = "high"
                me["review_tier"] = "high"
        check_fix_limit(candidate, {key})
        self._validate_change(candidate, before, affected={key})
        assignments = [field for field in edit_fields if field != "depends_on"]
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
                    edit_fields.get("review_tier", fresh["review_tier"]),
                    edit_fields.get("evidence_kinds", fresh["evidence_kinds"]))
                # Bump the round like ``submit`` does: earlier verdicts stop
                # matching ``_round_verdicts`` -- no stale count under the new tier.
                round_no = int(fresh.get("round") or 0) + 1
            if assignments:
                sets = ", ".join(f"{field}=?" for field in assignments)
                values = [self._stored(field, edit_fields[field]) for field in assignments]
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
            if "depends_on" in edit_fields:
                self.conn.execute("DELETE FROM edges WHERE src=?", (key,))
                for dep in me["depends_on"]:
                    self.conn.execute(
                        "INSERT INTO edges(src,dst,rel) VALUES(?,?, 'depends_on')",
                        (key, dep))
            payload = _defined_payload(edit_fields)
            if auto_src:
                # The event says the source was written by this edit, not asked
                # for by the caller (``fold`` ignores the extra key).
                payload["estimate_src_auto"] = True
            if tier_edit:
                # D-434 marker: the state the router re-tiered through; the
                # fold reads it to replay the round bump that voids verdicts.
                payload["while"] = fresh["state"]
            self._append(principal, key, "item_defined", payload, now)
            if has_priority:
                self._apply_priority(principal, key, prio, now)
        result = {"key": key}
        if dropped:
            # Only when something was actually dropped: every caller (the HTTP
            # door, the CLI, apply_ops' results) keeps reading ``{"key": ...}``.
            result["warnings"] = dropped
        return result

    def set_priority(self, principal, key, n):
        return self._run(principal, "set_priority", key,
                         lambda now: self._priority(principal, key, n, now))

    def _priority(self, principal, key, n, now):
        self._require_role(principal, "router")
        self._checked_priority(n)
        with self._write():
            self._apply_priority(principal, key, n, now)
        return {"key": key, "priority": int(n)}

    @staticmethod
    def _checked_priority(n):
        if isinstance(n, bool) or not isinstance(n, int):
            raise Refused("validation", "priority must be an integer")
        return int(n)

    def _apply_priority(self, principal, key, n, now):
        # Read and assert inside the transaction: the item may have been
        # cancelled or marked DONE before we take the write lock, and must then
        # get no priority change and no event.  The guarded UPDATE proves the
        # row we read is the one we changed.
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        cursor = self.conn.execute(
            "UPDATE items SET priority=?, updated_at=?"
            " WHERE key=? AND state=? AND epoch=?",
            (int(n), self._iso(now), key, item["state"], item["epoch"]))
        self._row_guard(cursor, key, item["state"], item["epoch"])
        self._append(principal, key, "priority_set", {"priority": int(n)}, now)
