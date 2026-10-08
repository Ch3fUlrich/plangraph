"""Validation of the small first-class item fields (MODEL-HOST, D-677).

``host`` names the machine an item runs on (``workstation``, ``coding.vm``).  It
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
                "phase_kind", "fixes")
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
