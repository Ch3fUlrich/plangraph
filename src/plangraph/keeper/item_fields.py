"""Validation of the small first-class item fields (MODEL-HOST, D-677).

``host`` names the machine an item runs on (``workstation``, ``coding.vm``).  It
is a HOST, not a project: nullable, ``[a-z0-9._-]+``, at most ``HOST_CAP`` chars.

``wait`` says what an operator-waiting item waits for: ``answer`` (a
decision/question the operator must answer), ``task`` (a manual step the
operator must do), or null.
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
