"""The keeper HTTP API route tables, split out of ``api.py`` (X7c).

Pure data: which verbs are readable/writable, which default to text output, and
how a refusal code maps to an HTTP status.  Kept separate so ``api.py`` stays
within its size budget as new doors (``close_on_decision``,
``set_legacy_state``) are added.
"""
from __future__ import annotations

from .errors import Refused

__all__ = ["READ_VERBS", "WRITE_VERBS", "TEXT_DEFAULT", "STATUS",
           "ROUTER_VERBS", "READ_CALLS"]

READ_VERBS = {"ready", "show", "blocked", "waves", "critical_path", "percent",
              "alerts", "digest", "snapshot", "whoami", "aliases", "needs_you",
              "answers", "progress"}
WRITE_VERBS = {"create_items", "import_items", "edit_definition",
               "set_priority", "claim", "delegate", "heartbeat", "release",
               "add_evidence", "add_evidence_router", "submit", "verdict",
               "cancel", "reap", "waive_second_family", "close_on_decision",
               "set_legacy_state", "apply_ops", "migrate_descriptions",
               "rehome", "record_run", "answer", "mint_run_token"}
TEXT_DEFAULT = {"ready", "show", "alerts", "blocked", "aliases"}

#: Simple read verbs: ``(store, principal, project) -> data``; the text formatter
#: is ``fmt.<name>_text``.
READ_CALLS = {
    "aliases": lambda store, who, project: store.aliases(who),
    "whoami": lambda store, who, project: store.whoami(who),
    "needs_you": lambda store, who, project: store.needs_you(who, project=project),
    "answers": lambda store, who, project: store.answers(who, project=project),
    "progress": lambda store, who, project: store.progress(who, project=project),
}

#: M2B: the only verbs a run token may even reach (the store narrows per role).
RUN_VERBS = frozenset({"heartbeat", "add_evidence", "submit", "verdict"})


def run_gate(principal, name):
    """A run token is refused (403) before any body parsing outside its verbs."""
    if getattr(principal, "token_id", None) is not None and name not in RUN_VERBS:
        raise Refused("forbidden", "a run token may not call this verb")


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


def _rehome(store, principal, body, need):
    # the store checks the role first, then new_project (so a non-router gets 403)
    return store.rehome(principal, need(body, "key"), body.get("new_project"),
                        body.get("host"))


def _record_run(store, principal, body, need):
    return store.record_run(principal, need(body, "key"), body.get("run"))


def _answer(store, principal, body, need):
    return store.answer(principal, need(body, "key"), body.get("text"),
                        body.get("decision_id"))


def _mint_run_token(store, principal, body, need):
    # router or an l1 of the item's project: the store decides which
    return store.mint_run_token(principal, need(body, "key"),
                                need(body, "role"), need(body, "run_id"))


ROUTER_VERBS = {"mint_run_token": _mint_run_token,
                "close_on_decision": _close_on_decision,
                "answer": _answer,
                "rehome": _rehome,
                "migrate_descriptions": _migrate_descriptions,
                "set_legacy_state": _set_legacy_state,
                "apply_ops": _apply_ops,
                "record_run": _record_run}
