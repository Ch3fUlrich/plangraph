"""Normalise item definitions and transport requests to the Rust owner gate."""
from __future__ import annotations
import os
from . import auth
from .placement_rules_bridge import call_operator
from .credential_guard import GUARDED_FIELDS, guard
from .errors import Refused
from .item_fields import (MODEL_FIELDS, check_choices, check_host, check_kg_refs, check_kind, check_model_fields,
                          check_wait, kind_defaults)
from .note_fold import DESCRIPTION_CAP, NOTE_CAP
_TEXT_CAPS = {"title": 200, "note": NOTE_CAP, "done_criteria": 600,
              "description": DESCRIPTION_CAP, "owner": 100, "track": 100,
              "estimate_src": 64}
_MAX_DEPS = 200
class InputMixin:
    """``_check_text``, ``_normalise`` and ``_qualify`` for :class:`Store`."""
    @staticmethod
    def _check_text(fields, key=None):
        """Validate text caps and credentials, identifying the item."""
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
        if "host" in fields:
            check_host(fields["host"])
        if "wait" in fields:
            check_wait(fields["wait"])
    def _normalise(self, raw):
        project = raw.get("project")
        identifier = raw.get("id")
        key = f"{project}:{identifier}" if project and identifier else None
        self._check_text(raw, key)
        if not project or not identifier:
            raise Refused("validation", "each item needs project and id")
        if raw.get("key") and raw["key"] != key:
            raise Refused("validation", f"key {raw['key']!r} != {key!r}")
        raw_deps = raw.get("depends_on") or []
        if not isinstance(raw_deps, (list, tuple)):
            raise Refused("validation", "depends_on must be a list")
        if len(raw_deps) > _MAX_DEPS:
            raise Refused("validation", f"depends_on exceeds {_MAX_DEPS} entries")
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
        under = raw.get("under")
        if under is not None:
            if not isinstance(under, str) or not under:
                raise Refused("validation", "under must be a non-empty string")
            under = self._qualify(project, under)
        return {
            "key": key, "project": project, "id": identifier,
            "title": raw.get("title", ""), "description": raw.get("description"),
            "host": raw.get("host"), "wait": wait, "kind": kind,
            "risk_tag": raw.get("risk_tag"), "until": raw.get("until"),
            **{name: raw.get(name) for name in MODEL_FIELDS},
            "track": raw.get("track"), "owner": raw.get("owner"),
            "owner_family": auth.norm_family(raw.get("owner_family")),
            "estimate_h": raw.get("estimate_h"), "estimate_src": raw.get("estimate_src"),
            "done_criteria": raw.get("done_criteria"),
            "evidence_kinds": list(raw.get("evidence_kinds") or ["any"]),
            "operator": bool(operator),
            "priority": int(raw.get("priority") or 0),
            "source": raw.get("source"), "note": self._folded_note(raw, key),
            "corrects": corrects, "fixes": fixes, "depends_on": deps,
            "under": under,
            "choices": check_choices(raw.get("choices"), kind, key),
            "kg_refs": check_kg_refs(raw.get("kg_refs"), key),
            "state": raw.get("state", "open"),
        }
    def _qualify(self, project, dep):
        """``project:dep`` for a bare id, then through the alias table."""
        return self.resolve_key(dep if ":" in dep else f"{project}:{dep}")
    def _operator_owner_gate(self, after, before=None, now=None):
        if not any(item and (item.get("operator") or item.get("owner") == "operator"
                             or item.get("risk_tag") or item.get("until"))
                   for item in (after, before)):
            return []
        events = [] if before is None else [dict(row) for row in self.conn.execute(
            "SELECT seq,type,actor,role,payload FROM events WHERE key=? ORDER BY seq DESC LIMIT 200",
            (before["key"],))]
        result = call_operator({"after": after, "before": before, "events": events,
            "operator_principal": os.environ.get("PG_OPERATOR_PRINCIPAL", "operator")})
        warnings = result.get("warnings", [])
        if result.get("refused"):
            raise Refused("operator_owner_gate", "; ".join(warnings))
        return warnings

    def _operator_hidden(self, items, now=None):
        if not any(item.get("until") for item in items):
            return set()
        result = call_operator({"items": [{"key": i["key"], "until": i.get("until")} for i in items], "today": self._iso(
            self.clock() if now is None else now)[:10]})
        return set(result.get("hidden", []))
