"""The keeper store core (M2a): open a DB, authenticate, drive the plan graph.

Public surface for other keeper modules:

* :func:`plangraph.keeper.db.open_db` / :func:`~plangraph.keeper.db.lock_keeper`
* :class:`~plangraph.keeper.auth.Principal` and the auth helpers
* :class:`~plangraph.keeper.store.Store`
* :class:`~plangraph.keeper.evidence_types.Result`
"""
import importlib

# Names load on first use (PEP 562): ``plangraph.keeper.mcp`` is a pure HTTP
# client and must import where the store core cannot (no ``fcntl`` on Windows,
# PG-WIN-MCP / D-884).
_LAZY = {
    "Principal": "auth", "add_principal": "auth", "authenticate": "auth",
    "revoke": "auth",
    "KeeperLock": "db", "lock_keeper": "db", "open_db": "db",
    "KeeperLocked": "errors", "Refused": "errors",
    "Result": "evidence_types",
    "Store": "store",
}

__all__ = [
    "Store", "Principal", "Result", "Refused", "KeeperLocked",
    "open_db", "lock_keeper", "add_principal", "authenticate", "revoke",
]


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
