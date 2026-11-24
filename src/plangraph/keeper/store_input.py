"""Item definition validation, normalisation, and criteria amendment audits.

Split out of ``store.py`` (size budget): the text caps, the credential guard,
the host check and the ``_normalise`` that turns one raw create/import dict into
the definition shape.  Dependency and ``corrects`` keys are resolved through the
alias table, so a definition may still name a key that has since been rehomed.
Criteria amendments are recorded on edits and verified against definition events.
"""
from __future__ import annotations

import json
import re

from . import auth
from .credential_guard import GUARDED_FIELDS, guard
from .errors import Refused
from .context_sources import decision_ids
from .item_fields import (MODEL_FIELDS, SMALL_FIELDS, check_choices, check_design_ref, check_host, check_kg_refs, check_kind, check_model_fields,
                          check_metadata, check_wait, kind_defaults)
from .note_fold import DESCRIPTION_CAP, NOTE_CAP

# Request-shape caps.  A field named in the refusal lets the caller fix it.
# ``estimate_src`` is the audit-meta provenance marker (PG-ESTIMATE-SRC-EDIT):
# a short label naming where the number came from, never a credential.
_TEXT_CAPS = {"title": 200, "note": NOTE_CAP, "done_criteria": 600,
              "description": DESCRIPTION_CAP, "owner": 100, "track": 100,
              "estimate_src": 64}
_MAX_DEPS = 200
GATE_DEPENDENCY_WARNING = 190
GATE_KINDS = ("gate", "goal")
_CRITERIA_AMENDMENT_FIELDS = ("criteria_reason", "criteria_evidence")
_LEGACY_SLICE_CRITERIA = re.compile(r"^Slice of [\w.-]+:[\w.-]+:")


def _unfinished_dependencies(keys, items):
    return {key for key in keys if items.get(key, {}).get("state") not in ("done", "cancelled")}


def citing_premise(values, names):
    """The premise texts among the fields *names* that cite a decision.

    Definition events carry the premise text the decision clock reads.  Only a
    field that cites an id is carried: the clock needs nothing else, and an event
    of an item that cites none keeps the payload (and hash) it always had.
    """
    return {name: values[name] for name in names
            if isinstance(values.get(name), str) and decision_ids(values[name])}


def _defined_payload(fields, before=None):
    """``item_defined`` payload: the changed field names plus the new tier value.

    The fold replays this to reconstruct ``review_tier``; it needs the value,
    not just the name, so ``verify_integrity`` can agree with the table.  The
    estimate pair rides along for the same reason: an event must say what the
    edit actually stored.  ``fold`` ignores names it does not compare
    (``estimate_h``/``estimate_src`` are definition columns, not cached ones),
    so carrying them keeps the event honest without touching the replay.
    """
    payload = {"fields": sorted(fields)}
    if "done_criteria" in fields:
        payload["criteria_ledger"] = True
    if "review_tier" in fields:
        payload["review_tier"] = fields["review_tier"]
    for name in SMALL_FIELDS:
        if name in fields:
            payload[name] = fields[name]
    payload.update(citing_premise(fields, ("title", "note")))   # done_criteria rides in before/after
    named = [name for name in ("title", "note", "description") if name in fields]
    if named and before is not None:
        # An edit that lists a field is not a change of it: the clock moves the anchor of the
        # item's fallback watermark only for the premise fields whose text really changed.  An
        # edit that changed every field it lists keeps the payload (and hash) it always had.
        same = [name for name in named if fields[name] == before.get(name)]
        if same:
            payload["premise_unchanged"] = same
    for name in ("estimate_h", "estimate_src"):
        if name in fields:
            payload[name] = fields[name]
    return payload


def verify_criteria_amendments(conn, all_events):
    """Check criteria edits covered by the append-only ledger."""
    amendments = {(row["key"], row["seq"]): (row["original_text"], row["new_text"])
                  for row in conn.execute(
                      "SELECT key,seq,original_text,new_text FROM criteria_amendments")}
    # The first row covers older ledger writers that did not mark their events.
    first_seq = min((seq for _, seq in amendments), default=None)
    field = "done_criteria"
    for event in all_events:
        payload = event["payload"]
        if event["type"] != "item_defined" or field not in payload.get("fields", ()):
            continue
        key, seq = event["key"], event["seq"]
        if not payload.get("criteria_ledger") and (first_seq is None or seq < first_seq):
            continue
        amendment = amendments.get((key, seq))
        before, after = payload.get("before", {}), payload.get("after", {})
        if field not in before or field not in after:
            if amendment is None:
                yield {"key": key, "seq": seq, "code": "criteria_amendment_unverifiable",
                       "field": field, "expected": "before and after values", "got": None}
            continue
        expected = (before[field], after[field])
        if expected[0] == expected[1]:
            continue
        if amendment != expected:
            code = "criteria_amendment_missing" if amendment is None else "criteria_amendment_mismatch"
            yield {"key": key, "seq": seq, "code": code, "field": field,
                   "expected": expected, "got": amendment}


class InputMixin:
    """``_check_text``, ``_normalise`` and ``_qualify`` for :class:`Store`."""

    @staticmethod
    def _criteria_amendment_fields(fields, key):
        if not isinstance(fields, dict) or "done_criteria" not in fields:
            return fields, {}
        fields = dict(fields)
        amendment = {}
        for name in _CRITERIA_AMENDMENT_FIELDS:
            if name in fields:
                value = fields.pop(name)
                if not isinstance(value, str) or not value.strip() or len(value) > 600:
                    raise Refused("validation", f"{name} must contain 1–600 characters")
                guard(name, value, key)
                amendment[name] = value
        return fields, amendment

    @staticmethod
    def _check_criteria_amendment(item, fields, amendment):
        if "done_criteria" in fields and fields["done_criteria"] != item["done_criteria"]:
            for name in _CRITERIA_AMENDMENT_FIELDS:
                if name not in amendment:
                    raise Refused("validation", f"{name} is required when done_criteria changes")

    def _record_criteria_amendment(self, seq, principal, item, fields, amendment, now):
        original = item.get("done_criteria")
        if "done_criteria" not in fields or original == fields["done_criteria"]:
            return
        self.conn.execute(
            "INSERT INTO criteria_amendments"
            "(seq,key,original_text,new_text,reason,evidence,authority,ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (seq, item["key"], original, fields["done_criteria"],
             amendment["criteria_reason"], amendment["criteria_evidence"],
             principal.name, self._iso(now)))

    @staticmethod
    def _check_text(fields, key=None):
        """Refuse oversize, non-string or credential-shaped text, naming the field.

        *key* (when known) prefixes a credential refusal so a batch can tell
        which item failed (PL-33)."""
        for field in GUARDED_FIELDS:
            guard(field, fields.get(field), key)
        for field, cap in _TEXT_CAPS.items():
            value = fields.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise Refused("validation", f"{field} must be a string")
            if len(value) > cap:
                raise Refused("validation", f"{field} exceeds {cap} characters")
        check_design_ref(fields.get("design_ref"), key)
        if "host" in fields:
            check_host(fields["host"])
        if "wait" in fields:
            check_wait(fields["wait"])

    def _check_slice_parent(self, principal, key):
        parent = self._get_or_refuse(key)
        self._may_manage(principal, parent["project"])
        # Rehoming creates a new key, but the original creation event still
        # identifies the slice.
        history = self.conn.execute(
            "WITH RECURSIVE lineage(key) AS ("
            " SELECT key FROM items WHERE key=? UNION"
            " SELECT a.old_key FROM aliases a JOIN lineage l ON a.new_key=l.key"
            ") SELECT e.payload FROM events e JOIN lineage l ON e.key=l.key"
            " WHERE e.type='item_created'", (key,))
        if _LEGACY_SLICE_CRITERIA.match(parent.get("done_criteria") or "") or any(
                json.loads(row["payload"]).get("slice_parent") for row in history):
            raise Refused("validation", f"parent {key} is already a slice")

    def _normalise(self, raw):
        project = raw.get("project")
        identifier = raw.get("id")
        # The item key is known before any check: name it in a credential
        # refusal so a batch of 40 items says which one failed (PL-33).
        key = f"{project}:{identifier}" if project and identifier else None
        self._check_text(raw, key)
        if not project or not identifier:
            raise Refused("validation", "each item needs project and id")
        if raw.get("key") and raw["key"] != key:
            raise Refused("validation", f"key {raw['key']!r} != {key!r}")
        raw_deps = raw.get("depends_on") or []
        if not isinstance(raw_deps, (list, tuple)):
            raise Refused("validation", "depends_on must be a list")
        deps = []
        for dep in raw_deps:
            if not isinstance(dep, str):
                raise Refused("validation",
                              "depends_on entries must be strings")
            deps.append(self._qualify(project, dep))
        kind = check_kind(raw.get("kind", "lane"), key)
        operator, wait = kind_defaults(raw, kind)
        check_wait(wait, key)
        check_model_fields(raw, kind, key)
        corrects = raw.get("corrects")
        if corrects:
            corrects = self._qualify(project, corrects)
        fixes = raw.get("fixes")
        if fixes is not None:
            if not isinstance(fixes, str) or not fixes:
                raise Refused("validation", f"{key}: fixes must be a key string or null")
            fixes = self._qualify(project, fixes)
        # ``under`` is create-only (``import_items`` refuses it): the gate the
        # new item is attached under, resolved like any other key reference and
        # kept on the definition -- never a column, never folded into ``note``.
        under = raw.get("under")
        if under is not None:
            if not isinstance(under, str) or not under:
                raise Refused("validation", "under must be a non-empty string")
            under = self._qualify(project, under)
        slice_parent = raw.get("slice_parent")
        if slice_parent is not None:
            if not isinstance(slice_parent, str) or not slice_parent.strip():
                raise Refused("validation", f"{key}: slice_parent must be a non-empty key")
            slice_parent = self._qualify(project, slice_parent)
        return {
            "key": key, "project": project, "id": identifier,
            "metadata": check_metadata(raw, key),
            "title": raw.get("title", ""), "description": raw.get("description"),
            "host": raw.get("host"), "wait": wait, "kind": kind,
            **{name: raw.get(name) for name in MODEL_FIELDS},
            "track": raw.get("track"), "owner": raw.get("owner"),
            "owner_family": auth.norm_family(raw.get("owner_family")),
            "estimate_h": raw.get("estimate_h"), "estimate_src": raw.get("estimate_src"),
            "done_criteria": raw.get("done_criteria"), "design_ref": raw.get("design_ref"),
            "evidence_kinds": list(raw.get("evidence_kinds") or ["any"]),
            "operator": bool(operator),
            "priority": int(raw.get("priority") or 0),
            "source": raw.get("source"), "note": self._folded_note(raw, key),
            "corrects": corrects, "fixes": fixes, "depends_on": deps,
            "under": under, "slice_parent": slice_parent,
            "choices": check_choices(raw.get("choices"), kind, key),
            "kg_refs": check_kg_refs(raw.get("kg_refs"), key),
            "state": raw.get("state", "open"),
        }

    def _check_definition_dependency_caps(self, definitions, attachments):
        if not attachments and not any(item["depends_on"] for item in definitions):
            return
        items = {item["key"]: item for item in self._all_items()}
        items.update((item["key"], item) for item in definitions)
        for item in definitions:
            self._check_dependency_cap(item["key"], item["depends_on"], items)
        additions = {}
        for item, gate in attachments:
            additions.setdefault(gate["key"], set()).add(item["key"])
        for key, added in additions.items():
            deps = set(items[key]["depends_on"]) | added
            self._check_dependency_cap(key, deps, items, attach_count=len(added))

    @staticmethod
    def _check_dependency_cap(key, deps, items, previous=None, *, attach_count=0):
        """Count distinct unfinished targets, permitting removal from legacy overages."""
        active = _unfinished_dependencies(deps, items)
        if len(active) <= _MAX_DEPS:
            return
        if previous is not None and active <= _unfinished_dependencies(previous, items):
            return
        detail = f"{key}: depends_on has {len(active)} open dependencies and exceeds the limit of {_MAX_DEPS}"
        if attach_count and items[key]["kind"] in GATE_KINDS:
            parts = [f"{key}-{suffix}" for suffix in ("A", "B")]
            for part in parts:
                item = items.get(part)
                if (item and item["kind"] in GATE_KINDS and item["state"] == "open"
                        and len(_unfinished_dependencies(item["depends_on"], items)) + attach_count <= _MAX_DEPS):
                    raise Refused("validation", f"{detail}; use split gate {part}")
            detail += f"; use a split gate with room ({' or '.join(parts)})"
        raise Refused("validation", detail)

    def _qualify(self, project, dep):
        """``project:dep`` for a bare id, then through the alias table."""
        return self.resolve_key(dep if ":" in dep else f"{project}:{dep}")
