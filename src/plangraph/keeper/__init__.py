"""The keeper store core (M2a): open a DB, authenticate, drive the plan graph.

Public surface for other keeper modules:

* :func:`plangraph.keeper.db.open_db` / :func:`~plangraph.keeper.db.lock_keeper`
* :class:`~plangraph.keeper.auth.Principal` and the auth helpers
* :class:`~plangraph.keeper.store.Store`
* :class:`~plangraph.keeper.evidence_types.Result`
"""
from .auth import Principal, add_principal, authenticate, revoke
from .db import KeeperLock, lock_keeper, open_db
from .errors import KeeperLocked, Refused
from .evidence_types import Result
from .store import Store

__all__ = [
    "Store", "Principal", "Result", "Refused", "KeeperLocked",
    "open_db", "lock_keeper", "add_principal", "authenticate", "revoke",
]
