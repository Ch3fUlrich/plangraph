"""Request-shape checks and normalisation for item definitions.

Split out of ``store.py`` (size budget): the text caps, the credential guard,
the host check and the ``_normalise`` that turns one raw create/import dict into
the definition shape.  Dependency and ``corrects`` keys are resolved through the
alias table, so a definition may still name a key that has since been rehomed.
"""
from __future__ import annotations

from . import auth
from .credential_guard import GUARDED_FIELDS, guard
from .errors import Refused
from .item_fields import (MODEL_FIELDS, check_host, check_kind, check_model_fields,
                          check_wait, kind_defaults)
from .note_fold import DESCRIPTION_CAP, NOTE_CAP

# Request-shape caps.  A field named in the refusal lets the caller fix it.
_TEXT_CAPS = {"title": 200, "note": NOTE_CAP, "done_criteria": 600,
              "description": DESCRIPTION_CAP, "owner": 100, "track": 100}
_MAX_DEPS = 200


class InputMixin:
    """``_check_text``, ``_normalise`` and ``_qualify`` for :class:`Store`."""

    @staticmethod
    def _check_text(fields):
        """Refuse oversize, non-string or credential-shaped text, naming the field."""
        for field in GUARDED_FIELDS:
            guard(field, fields.get(field))
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
        self._check_text(raw)
        project = raw.get("project")
        identifier = raw.get("id")
        if not project or not identifier:
            raise Refused("validation", "each item needs project and id")
        key = f"{project}:{identifier}"
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
        return {
            "key": key, "project": project, "id": identifier,
            "title": raw.get("title", ""), "description": raw.get("description"),
            "host": raw.get("host"), "wait": wait, "kind": kind,
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
            "state": raw.get("state", "open"),
        }

    def _qualify(self, project, dep):
        """``project:dep`` for a bare id, then through the alias table."""
        return self.resolve_key(dep if ":" in dep else f"{project}:{dep}")
