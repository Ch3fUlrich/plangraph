"""The keeper HTTP API route tables, split out of ``api.py`` (X7c).

Pure data: which verbs are readable/writable, which default to text output, and
how a refusal code maps to an HTTP status.  Kept separate so ``api.py`` stays
within its size budget as new doors (``close_on_decision``,
``set_legacy_state``) are added.
"""
from __future__ import annotations

import os
import urllib.parse

from ..inbox import routes as _inbox
from .errors import Refused

__all__ = ["READ_VERBS", "WRITE_VERBS", "TEXT_DEFAULT", "STATUS",
           "ROUTER_VERBS", "READ_CALLS", "ROLE_CHECKED_VERBS",
           "WORKER_VERBS", "worker_write", "JEV_VERBS", "jev_write"]

READ_VERBS = {"ready", "show", "blocked", "waves", "critical_path", "percent",
              "alerts", "digest", "snapshot", "whoami", "aliases", "needs_you",
              "answers", "progress", "changelog", "audit-meta", "metrics",
              "jev_report"}
WRITE_VERBS = {"create_items", "import_items", "edit_definition",
               "set_priority", "claim", "delegate", "heartbeat", "release",
               "add_evidence", "add_evidence_router", "submit", "verdict",
               "cancel", "reap", "waive_second_family", "close_on_decision",
               "set_legacy_state", "apply_ops", "migrate_descriptions",
               "rehome", "record_run", "answer", "mint_run_token", "replay",
                "withdraw_review", "reopen", "set_kind", "set_metrics", "requeue",
               # phase_plan is POST transport only; the verb is read-only (planner/phase.py)
               "governor_override", "phase_plan",
               # phase_finish mutates only GitHub, never the store; transport is POST
               "phase_finish",
               # worker_claim is POST transport: it returns a Claim to a worker
               "worker_claim",
               "attempts", "park", "complete", "alarms", "locks",
               "jev_shadow", "jev_outcome", "log_item", "rule_set"}
READ_VERBS |= _inbox.READ_VERBS      # RT-P1-INBOX-CODE: questions
WRITE_VERBS |= _inbox.WRITE_VERBS    # ask_question, answer_question, resignal_question
TEXT_DEFAULT = {"ready", "show", "alerts", "blocked", "aliases", "changelog",
                "audit-meta"}

#: Simple read verbs: ``(store, principal, project) -> data``; the text formatter
#: is ``fmt.<name>_text``.
READ_CALLS = {
    "aliases": lambda store, who, project: store.aliases(who),
    "whoami": lambda store, who, project: store.whoami(who),
    "needs_you": lambda store, who, project: store.needs_you(who, project=project),
    "answers": lambda store, who, project: store.answers(who, project=project),
    "progress": lambda store, who, project: store.progress(who, project=project),
    "jev_report": lambda store, who, project: store.jev_report(who, project=project),
}

#: M2B: the only verbs a run token may even reach (the store narrows per role).
RUN_VERBS = frozenset({"heartbeat", "add_evidence", "submit", "verdict", "replay"})


#: The roles each inline ``_write`` verb can ever admit (a superset of the
#: store's own check -- claimant verbs are identity-gated, so every role that can
#: hold a claim).  A caller outside this set is refused (audited, 403) before
#: its body is validated; a caller inside it keeps the 400 validation.
_CLAIMANT = ("l1", "worker", "system")
INLINE_ROLES = {
    "create_items": ("router", "l1", "oversight"),
    "import_items": ("router",),
    "edit_definition": ("router", "l1", "oversight", "worker"),
    "set_priority": ("router", "oversight"),
    "claim": ("l1", "worker"),
    "delegate": _CLAIMANT,
    "heartbeat": _CLAIMANT,
    "release": _CLAIMANT + ("router",),
    "submit": _CLAIMANT,
    "add_evidence": _CLAIMANT,
    "add_evidence_router": ("router", "system"),
    "verdict": ("reviewer",),
    "cancel": ("router", "l1", "oversight"),
    "waive_second_family": ("router",),
}


def run_gate(principal, name):
    """A run token is refused (403) before any body parsing outside its verbs."""
    if getattr(principal, "token_id", None) is not None and name not in RUN_VERBS:
        raise Refused("forbidden", "a run token may not call this verb")


STATUS = {"forbidden": 403, "not_found": 404, "conflict": 409, "locked": 409,
          "epoch_stale": 409, "lease_held": 409, "same_principal": 409,
          "same_family": 409, "unavailable": 503, "exec_stale": 409,
          "bad_transition": 409, "exec_stale_state": 409}


#: RT-P1-KEEPER: the Rust worker's verbs (rust/worker/src/keeper.rs).
WORKER_VERBS = frozenset({"attempts", "park", "complete", "alarms", "locks"})


def worker_write(store, principal, name, extra, body):
    """The worker verbs; a ``None`` result means 204 with an empty body.

    ``locks`` arrives as ``POST /v1/locks/<repo>/<action>``: ``extra`` is the
    percent-encoded single repo segment joined with the action, so split on
    "/" first and unquote the repo after.
    """
    if name != "locks":
        if not isinstance(body, dict):
            raise Refused("validation", "body must be a JSON object")
        if principal.role == "worker" and "role" in body:
            raise Refused("forbidden", "body cannot override token role")
        if name == "attempts":
            store.worker_attempt(principal, body)
        elif name == "park":
            store.worker_park(principal, body)
        elif name == "complete":
            store.worker_complete(principal, body)
        else:
            store.worker_alarm(principal, body)
        return None
    parts = extra.split("/")
    if len(parts) != 2:
        raise Refused("not_found", f"unknown locks route /v1/locks/{extra}")
    repo, action = urllib.parse.unquote(parts[0]), parts[1]
    if not repo:
        raise Refused("not_found", "locks needs a repo segment")
    if action == "acquire":
        return store.lock_acquire(principal, repo, body)
    if action == "release":
        store.lock_release(principal, repo, body)
        return None
    raise Refused("not_found", f"unknown locks route /v1/locks/{extra}")


#: RT-P1-JEV-SHADOW: the Jev shadow write verbs.  The store resolves the card
#: from ``body["card"]`` itself, so the handler must NOT rewrite ``body["key"]``.
JEV_VERBS = frozenset({"jev_shadow", "jev_outcome"})


def jev_write(store, principal, name, body):
    """The Jev shadow writes; the store answers ``{id,card,kind}`` / ``{card,updated}``.

    The role is checked by the store BEFORE the body is validated, so a reader
    gets 403 rather than 400 for a malformed body.
    """
    if not isinstance(body, dict):
        raise Refused("validation", "body must be a JSON object")
    if name == "jev_shadow":
        return store.jev_record(principal, body)
    return store.jev_outcome(principal, body)


# Routed verbs: ``(store, principal, body, need) -> result``.  ``need`` is the
# handler's required-field reader.  PG-EFFICIENCY-GATES fix round 2 (qwen-R1):
# a handler never checks the role (the store is the single role authority) and
# never raises on the body before the store's audited ``Store._run`` seam has
# gated the role -- the body reads below run inside the seam's ``fn``, so a
# wrong role is refused 403 and audited (ok=0, code forbidden) BEFORE any
# body-shape or required-field validation.
def _close_on_decision(store, principal, body, need):
    def work(now):
        return store._close_on_decision(principal, need(body, "key"),
                                        need(body, "batch_ref"), now)
    return store._run(principal, "close_on_decision", body.get("key"), work)


def _withdraw_review(store, principal, body, need):
    return store.withdraw_review(principal, body.get("key"), body.get("decision_id"))  # role first (403)


def _reopen(store, principal, body, need):
    return store.reopen(principal, body.get("key"), body.get("reason"),
                        body.get("decision_id"))   # role first (403)


def _set_legacy_state(store, principal, body, need):
    def work(now):
        return store._set_legacy_state(principal, need(body, "key"),
                                       need(body, "state"),
                                       body.get("note", ""), now)
    return store._run(principal, "set_legacy_state", body.get("key"), work)


#: D-1498/D-2760: an unknown body key is refused, never ignored (an old keeper
#: that ignored ``dry_run`` applied a "dry run" batch for real).
SET_KIND_FIELDS = frozenset({"key", "kind"})
APPLY_OPS_FIELDS = frozenset({"ops", "batch_ref", "base_seq", "dry_run"})


def _set_kind(store, principal, body, need):
    def work(now):
        extra = set(body) - SET_KIND_FIELDS
        if extra:
            raise Refused("validation", f"unexpected fields {sorted(extra)}")
        return store._set_kind(principal, need(body, "key"),
                               body.get("kind"), now)
    return store._run(principal, "set_kind", body.get("key"), work)


def _apply_ops(store, principal, body, need):
    def work(now):
        # Strict body (D-1498/D-2760) before the ops read, for callers the
        # verb admits -- a role refusal can never be masked by body validation
        # because the role gate already ran before this fn.
        extra = set(body) - APPLY_OPS_FIELDS
        if extra:
            raise Refused("validation", f"unexpected fields {sorted(extra)}")
        return store._apply_ops_audited(principal, need(body, "ops"),
                                        body.get("batch_ref"), now,
                                        base_seq=body.get("base_seq"),
                                        dry_run=body.get("dry_run"))
    return store._run(principal, "apply_ops", None, work)


def _migrate_descriptions(store, principal, body, need):
    return store.migrate_descriptions(principal)


def _rehome(store, principal, body, need):
    def work(now):
        return store._rehome(principal, need(body, "key"),
                             body.get("new_project"), body.get("host"), now)
    return store._run(principal, "rehome", body.get("key"), work)


def _record_run(store, principal, body, need):
    def work(now):
        return store._record_run(principal, need(body, "key"),
                                 body.get("run"), now)
    return store._run(principal, "record_run", body.get("key"), work)


def _answer(store, principal, body, need):
    def work(now):
        return store._answer(principal, need(body, "key"), body.get("text"),
                             body.get("decision_id"), now)
    return store._run(principal, "answer", body.get("key"), work)


def _mint_run_token(store, principal, body, need):
    # router or an l1 of the item's project: the store decides which (the
    # project check follows the body reads inside the store).
    def work(now):
        return store._mint(principal, need(body, "key"), need(body, "role"),
                           need(body, "run_id"), now)
    return store._run(principal, "mint_run_token", body.get("key"), work)


def _set_metrics(store, principal, body, need):
    return store.set_metrics(principal, body.get("day"), body.get("items"))   # role first (403)


def _governor_override(store, principal, body, need):
    return store.log_governor_override(principal, body.get("start_kind"),
                                       body.get("initiator"), body.get("reason"),
                                       body.get("provider"))   # role first (403)


def _replay(store, principal, body, need):
    return store.replay(principal, body.get("entries"))   # M2B-OUTBOX: principal checked first (403)


def _phase_plan(store, principal, body, need):
    def work(_now):
        # The role gate (Store._run, ROLE_GATE) already refused a wrong role
        # before any env or key check; plan_phase re-checks the role itself too.
        from ..planner.phase import plan_phase   # local import: keeps keeper leaf-free
        key = need(body, "key")
        repo_root = os.environ.get("PG_PHASE_REPO_ROOT")
        if not repo_root or not os.path.isdir(repo_root):
            raise Refused("unavailable", "PG_PHASE_REPO_ROOT is not configured")
        # repo_root is server-side only.
        return plan_phase(store, principal, key, repo_root=repo_root, now=None)
    return store._run(principal, "phase_plan", body.get("key"), work)

#: Seam for tests: the keeper's own GitHub adapter, built from the server environment only.
def _github_from_env():
    from ..planner.github_adapter import RestGitHub   # local import: keeps keeper leaf-free
    return RestGitHub.from_env()

def _phase_finish(store, principal, body, need):
    def work(_now):
        # The role gate (Store._run, ROLE_GATE) already refused a wrong role
        # before env, key or GitHub access checks; finish_phase re-checks too.
        from ..planner.phase_finish import finish_phase
        key, branch, head = need(body, "key"), need(body, "branch"), need(body, "head")
        repo_root = os.environ.get("PG_PHASE_REPO_ROOT")
        if not repo_root or not os.path.isdir(repo_root):
            raise Refused("unavailable", "PG_PHASE_REPO_ROOT is not configured")
        github = _github_from_env()
        if github is None:
            raise Refused("unavailable", "phase_finish GitHub access is not configured")
        # repo_root, the repo slug and the token file come from the server environment only, never the body.
        return finish_phase(store, principal, key, branch, head, repo_root=repo_root, github=github)
    return store._run(principal, "phase_finish", body.get("key"), work)

def _worker_claim(store, principal, body, need):
    def work(_now):
        # The role gate (Store._run, ROLE_GATE) already refused a wrong role
        # before key or env access checks; worker_claim re-checks the role too.
        key = need(body, "card")
        # body.worker is informational (a worker process id) and is ignored.
        # The calling principal from the token is the only identity.
        # Repo root and base branch come from the server environment only, never the body.
        repo_root = os.environ.get("PG_PHASE_REPO_ROOT")
        base_branch = os.environ.get("PG_PHASE_BASE_BRANCH") or "main"
        from ..planner.worker_claim import worker_claim
        return worker_claim(
            store, principal, key, repo_root=repo_root, base_branch=base_branch
        )
    return store._run(principal, "worker_claim", body.get("card"), work)


def _requeue(store, principal, body, need):
    def work(now):
        return store._requeue(principal, need(body, "key"),
                              body.get("reason", ""), now)
    return store._run(principal, "requeue", body.get("key"), work)


def _log_item(store, principal, body, need):
    def work(now):
        return store._log_item(principal, need(body, "key"),
                               body.get("entry"), now)
    return store._run(principal, "log_item", body.get("key"), work)


def _rule_set(store, principal, body, need):
    def work(now):
        return store._rule_set(principal, need(body, "rules"), now)
    return store._run(principal, "rule_set", None, work)


ROUTER_VERBS = {"mint_run_token": _mint_run_token,
                "replay": _replay,
                "close_on_decision": _close_on_decision,
                "withdraw_review": _withdraw_review,
                "reopen": _reopen,
                "set_kind": _set_kind,
                "requeue": _requeue,
                "answer": _answer,
                "rehome": _rehome,
                "migrate_descriptions": _migrate_descriptions,
                "set_legacy_state": _set_legacy_state,
                "apply_ops": _apply_ops,
                "record_run": _record_run}


#: Write verbs whose STORE path checks the role itself and admits more than the
#: router (set_metrics: router, system; governor_override: router, l1, system,
#: spawner), so they are not in ROUTER_VERBS.  The role lists live only in the
#: store (``store.ROLE_GATE`` / the store method's own first check) -- this
#: table names verbs, never roles (qwen-R2).
ROLE_CHECKED_VERBS = {"set_metrics": _set_metrics,
                      "governor_override": _governor_override,
                      "phase_plan": _phase_plan,
                      "phase_finish": _phase_finish,
                      "worker_claim": _worker_claim,
                      # PG-EFFICIENCY-GATES: router or the project L1
                      # (store.ROLE_GATE + store._log_item's project check)
                      "log_item": _log_item,
                      # PG-EFFICIENCY-GATES slice 2 (D-2746): router only
                      # (store.ROLE_GATE)
                      "rule_set": _rule_set}
