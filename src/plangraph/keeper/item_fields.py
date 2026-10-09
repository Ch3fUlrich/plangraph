"""Validation of the small first-class item fields (MODEL-HOST, D-677).

``host`` names the machine an item runs on (``workstation``, ``example-host``).  It
is a HOST, not a project: nullable, ``[a-z0-9._-]+``, at most ``HOST_CAP`` chars.

``wait`` says what an operator-waiting item waits for: ``answer`` (a
decision/question the operator must answer), ``task`` (a manual step the
operator must do), or null.

MODEL-KINDS adds four optional model-of-work fields: ``layer`` (L1/L2/L3), ``role``
(planner/implementer/reviewer/researcher/operator), ``agent_id`` (the agent that
runs the item, <= ``AGENT_ID_CAP`` chars of ``[A-Za-z0-9._:@-]``) and ``phase_kind``
(planning/work, gate items only).

MODEL-KINDS: ``KINDS`` lists the item kinds.  ``question``, ``decision`` and
``operator_task`` wait on the operator: ``kind_defaults`` gives their create-time
``operator`` / ``wait`` defaults (explicit values always win).
"""
from __future__ import annotations

import re

from .errors import Refused

HOST_CAP = 100
_HOST = re.compile(r"[a-z0-9._-]+")

#: D-677: the only legal ``wait`` values (plus null).
WAIT_VALUES = ("answer", "task")


def check_host(value, key=None):
    """Return *value* if it is ``None`` or a legal host, else refuse ``validation``."""
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if not isinstance(value, str):
        raise Refused("validation", f"{where}host must be a string")
    if len(value) > HOST_CAP:
        raise Refused("validation", f"{where}host exceeds {HOST_CAP} characters")
    if not _HOST.fullmatch(value):
        raise Refused("validation",
                      f"{where}host must match [a-z0-9._-]+ (lowercase, no spaces)")
    return value


def check_wait(value, key=None):
    """Return *value* if it is ``None``, ``"answer"`` or ``"task"``, else refuse."""
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if value not in WAIT_VALUES:
        raise Refused("validation",
                      f"{where}wait must be one of {list(WAIT_VALUES)} or null")
    return value


#: Optional columns folded from ``item_created``/``item_defined`` payloads.
SMALL_FIELDS = ("description", "host", "wait", "layer", "role", "agent_id",
                "phase_kind", "fixes", "choices")
#: The MODEL-KINDS subset of ``SMALL_FIELDS``.
MODEL_FIELDS = ("layer", "role", "agent_id", "phase_kind")

LAYERS = ("L1", "L2", "L3")
ROLES = ("planner", "implementer", "reviewer", "researcher", "operator")
PHASE_KINDS = ("planning", "work")
AGENT_ID_CAP = 128
_AGENT_ID = re.compile(r"[A-Za-z0-9._:@-]+")


def _enum(name, value, allowed, key):
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        where = f"{key}: " if key else ""
        raise Refused("validation",
                      f"{where}{name} must be one of {list(allowed)} or null")
    return value


def check_agent_id(value, key=None):
    """Return *value* if it is ``None`` or a legal agent id, else refuse."""
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if not isinstance(value, str):
        raise Refused("validation", f"{where}agent_id must be a string")
    if len(value) > AGENT_ID_CAP:
        raise Refused("validation",
                      f"{where}agent_id exceeds {AGENT_ID_CAP} characters")
    if not _AGENT_ID.fullmatch(value):
        raise Refused("validation",
                      f"{where}agent_id must match [A-Za-z0-9._:@-]+")
    return value


def check_model_fields(fields, kind, key=None):
    """Validate the MODEL-KINDS fields present in *fields* for an item of *kind*.

    ``phase_kind`` is refused on anything but a gate.  Absent names are skipped.
    """
    if "layer" in fields:
        _enum("layer", fields["layer"], LAYERS, key)
    if "role" in fields:
        _enum("role", fields["role"], ROLES, key)
    if "agent_id" in fields:
        check_agent_id(fields["agent_id"], key)
    if "phase_kind" in fields:
        value = _enum("phase_kind", fields["phase_kind"], PHASE_KINDS, key)
        if value is not None and kind != "gate":
            where = f"{key}: " if key else ""
            raise Refused("validation",
                          f"{where}phase_kind is only allowed on gate items")


#: Item kinds.  The last three wait on the operator and block dependents like a lane.
KINDS = ("lane", "gate", "goal", "question", "decision", "operator_task")
#: kind -> (operator, wait) used when the definition does not say.
KIND_DEFAULTS = {"question": (True, "answer"), "decision": (True, "answer"),
                 "operator_task": (True, "task")}


def check_kind(value, key=None):
    """Return *value* if it is one of ``KINDS``, else refuse ``validation``."""
    if not isinstance(value, str) or value not in KINDS:
        where = f"{key}: " if key else ""
        raise Refused("validation", f"{where}kind {value!r} is not one of {KINDS}")
    return value


def kind_defaults(raw, kind):
    """``(operator, wait)`` for a definition *raw* of *kind*.

    A key present in *raw* wins (``wait: null`` is an explicit "no wait"); an
    ``operator`` of null counts as absent.
    """
    op_default, wait_default = KIND_DEFAULTS.get(kind, (False, None))
    operator = raw.get("operator")
    return (op_default if operator is None else operator,
            raw["wait"] if "wait" in raw else wait_default)


# --------------------------------------------------------------------------- #
# QA-IMPORT (D-865): structured choices of an operator question / task.
CHOICE_KINDS = ("question", "decision", "operator_task")
MAX_OPTIONS = 8
OPTION_ID_CAP, LABEL_CAP, CONSEQUENCE_CAP, BLOCKS_CAP = 40, 60, 160, 200
_OPTION_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _choice_text(name, value, cap, where):
    if not isinstance(value, str) or not value.strip() or len(value) > cap:
        raise Refused("validation", f"{where}choices {name} must be 1-{cap} characters")
    if any(ord(c) < 32 for c in value):
        raise Refused("validation", f"{where}choices {name} must not contain control characters")
    from .credential_guard import guard
    guard(f"choices {name}", value)
    return value.strip()


def check_choices(value, kind, key=None):
    """Canonical JSON text of ``{options, default, deadline, blocks}`` or ``None``.

    ``options``: 1..MAX_OPTIONS ``{id, label, consequence}`` (ids unique, ``[a-z0-9-]``);
    ``default``: one option id or null; ``deadline``: ``YYYY-MM-DD`` or null;
    ``blocks``: short text or null.  Only question/decision/operator_task items.
    """
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if kind not in CHOICE_KINDS:
        raise Refused("validation", f"{where}choices only on {list(CHOICE_KINDS)} items")
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise Refused("validation", f"{where}choices must be an object") from exc
    if not isinstance(value, dict) or not set(value) <= {"options", "default", "deadline", "blocks"}:
        raise Refused("validation", f"{where}choices must be an object with options/default/deadline/blocks")
    out = {"options": [], "default": None, "deadline": None, "blocks": None}
    options = value.get("options") or []
    if not isinstance(options, list) or len(options) > MAX_OPTIONS:
        raise Refused("validation", f"{where}choices options must be a list of <= {MAX_OPTIONS}")
    seen = set()
    for opt in options:
        if not isinstance(opt, dict) or not set(opt) <= {"id", "label", "consequence"}:
            raise Refused("validation", f"{where}choices option must be {{id, label, consequence}}")
        oid = opt.get("id")
        if not isinstance(oid, str) or len(oid) > OPTION_ID_CAP or not _OPTION_ID.fullmatch(oid) \
                or oid in seen:
            raise Refused("validation", f"{where}choices option id must be unique [a-z0-9-]+")
        seen.add(oid)
        entry = {"id": oid, "label": _choice_text("label", opt.get("label"), LABEL_CAP, where)}
        if opt.get("consequence") is not None:
            entry["consequence"] = _choice_text("consequence", opt["consequence"],
                                                CONSEQUENCE_CAP, where)
        out["options"].append(entry)
    default = value.get("default")
    if default is not None and default not in seen:
        raise Refused("validation", f"{where}choices default must be one of the option ids")
    out["default"] = default
    deadline = value.get("deadline")
    if deadline is not None:
        if not isinstance(deadline, str) or not _DATE.fullmatch(deadline):
            raise Refused("validation", f"{where}choices deadline must be YYYY-MM-DD")
        from datetime import date
        try:
            date.fromisoformat(deadline)
        except ValueError as exc:
            raise Refused("validation", f"{where}choices deadline is not a date") from exc
    out["deadline"] = deadline
    if value.get("blocks") is not None:
        out["blocks"] = _choice_text("blocks", value["blocks"], BLOCKS_CAP, where)
    if not out["options"] and out["default"] is None and out["deadline"] is None \
            and out["blocks"] is None:
        return None
    import json
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def parse_choices(text):
    """The stored JSON text as a dict (``None`` when absent or unreadable)."""
    if not text:
        return None
    import json
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None
