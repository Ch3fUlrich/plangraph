"""Keeper exceptions shared by every module."""
from __future__ import annotations

__all__ = ["Refused", "KeeperLocked"]


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
