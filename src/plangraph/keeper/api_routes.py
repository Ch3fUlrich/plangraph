"""The keeper HTTP API route tables, split out of ``api.py`` (X7c).

Pure data: which verbs are readable/writable, which default to text output, and
how a refusal code maps to an HTTP status.  Kept separate so ``api.py`` stays
within its size budget as new doors (``close_on_decision``,
``set_legacy_state``) are added.
"""
from __future__ import annotations

__all__ = ["READ_VERBS", "WRITE_VERBS", "TEXT_DEFAULT", "STATUS",
           "ROUTER_VERBS"]

READ_VERBS = {"ready", "show", "blocked", "waves", "critical_path", "percent",
              "alerts", "digest", "snapshot", "whoami"}
WRITE_VERBS = {"create_items", "import_items", "edit_definition",
               "set_priority", "claim", "delegate", "heartbeat", "release",
               "add_evidence", "add_evidence_router", "submit", "verdict",
               "cancel", "reap", "waive_second_family", "close_on_decision",
               "set_legacy_state", "apply_ops", "migrate_descriptions"}
TEXT_DEFAULT = {"ready", "show", "alerts", "blocked"}

STATUS = {"forbidden": 403, "not_found": 404, "conflict": 409, "locked": 409,
          "epoch_stale": 409, "lease_held": 409, "same_principal": 409,
          "same_family": 409}


# Router-only verbs: ``(store, principal, body, need) -> result``.  ``need`` is
# the handler's required-field reader.
def _close_on_decision(store, principal, body, need):
    return store.close_on_decision(principal, need(body, "key"),
                                   need(body, "batch_ref"))


def _set_legacy_state(store, principal, body, need):
    return store.set_legacy_state(principal, need(body, "key"),
                                  need(body, "state"), body.get("note", ""))


def _apply_ops(store, principal, body, need):
    return store.apply_ops(principal, need(body, "ops"), body.get("batch_ref"))


def _migrate_descriptions(store, principal, body, need):
    return store.migrate_descriptions(principal)


ROUTER_VERBS = {"close_on_decision": _close_on_decision,
                "migrate_descriptions": _migrate_descriptions,
                "set_legacy_state": _set_legacy_state,
                "apply_ops": _apply_ops}
