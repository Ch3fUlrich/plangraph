"""Keeper exceptions shared by every module."""
from __future__ import annotations

__all__ = ["Refused", "KeeperLocked", "ConnectionLost"]


class Refused(Exception):
    """A verb was refused; ``code`` is one of the contract's error codes.

    Refusals are expected outcomes, not bugs: the store audits every one and
    the API maps them to 4xx responses.
    """

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.msg = msg


class KeeperLocked(RuntimeError):
    """Another keeper process holds the singleton ``lock_keeper`` lock."""


class ConnectionLost(Exception):
    """The Postgres session was lost; the request cannot continue (I3, fail-loud).

    ``phase`` is ``"statement"`` (a statement failed on a dead session) or
    ``"commit"`` (the open transaction's outcome is unknown).  The message is
    fixed text: driver errors can carry a host or DSN, so only the type name is
    ever logged (I10).
    """

    def __init__(self, phase: str = "statement"):
        super().__init__("keeper database connection lost")
        self.phase = phase
