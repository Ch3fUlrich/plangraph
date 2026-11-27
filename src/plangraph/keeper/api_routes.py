"""The keeper HTTP API route tables, split out of ``api.py`` (X7c).

Pure data: which verbs are readable/writable, which default to text output, and
how a refusal code maps to an HTTP status.  Kept separate so ``api.py`` stays
within its size budget as new doors (``close_on_decision``,
``set_legacy_state``) are added.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import nullcontext
from copy import copy, deepcopy
from time import monotonic
import urllib.parse

from ..inbox import routes as _inbox
from . import auth, timing
from .errors import Refused

__all__ = ["READ_VERBS", "WRITE_VERBS", "TEXT_DEFAULT", "STATUS",
           "ROUTER_VERBS", "READ_CALLS", "ROLE_CHECKED_VERBS",
           "WORKER_VERBS", "worker_write", "JEV_VERBS", "jev_write",
           "SWEEP_READ_VERBS", "check_sweep"]

READ_VERBS = {"ready", "show", "blocked", "waves", "critical_path", "percent",
              "alerts", "digest", "snapshot", "whoami", "aliases", "needs_you",
              "answers", "progress", "changelog", "audit-meta", "metrics",
              "jev_report", "write_context", "events", "attempts", "spawn_attempt_coverage"}
WRITE_VERBS = {"create_items", "import_items", "edit_definition",
               "set_priority", "claim", "claim_batch", "delegate", "heartbeat", "heartbeat_batch", "release",
               "add_evidence", "add_evidence_router", "incident", "submit", "verdict",
               "cancel", "reap", "waive_second_family", "close_on_decision",
               "set_legacy_state", "apply_ops", "migrate_descriptions",
               "grant_apply", "revoke_apply",
               "rehome", "record_run", "record_spawn_attempt", "answer", "mint_run_token", "replay",
                "withdraw_review", "reopen", "set_kind", "set_metrics", "requeue",
               "close_merged", "close_merged_auto",
               # phase_plan is POST transport only; the verb is read-only (planner/phase.py)
               "governor_override", "phase_plan",
               # phase_finish mutates only GitHub, never the store; transport is POST
               "phase_finish",
               # worker_claim is POST transport: it returns a Claim to a worker
               "worker_claim",
               "attempts", "park", "complete", "alarms", "locks",
               "jev_shadow", "jev_outcome"}
READ_VERBS |= _inbox.READ_VERBS      # RT-P1-INBOX-CODE: questions
READ_VERBS.add("messages")
READ_VERBS |= {"host_samples", "host_samples_range", "apply_grants"}
WRITE_VERBS |= {"ingest_host_sample", "mint_host_token", "revoke_host_token"}
WRITE_VERBS |= {"post_message", "land", "write_receipt", "admit_seat_verdict", "bind_seat_verdict"}
WRITE_VERBS |= _inbox.WRITE_VERBS    # ask_question, answer_question, resignal_question
TEXT_DEFAULT = {"ready", "show", "alerts", "blocked", "aliases", "changelog",
                "audit-meta"}


def dispatch_label(name, body):
    return "merge_refresh" if name == "close_merged_auto" and isinstance(body, dict) and "refresh" in body else name


def committed_messages(server, principal):
    return getattr(server, "read_path", None) and not isinstance(principal, auth.OversightPrincipal)


def send_messages(handler, principal, query):
    from .messages_poll import read_messages
    run_gate(principal, "messages")
    with timing.RequestTimer(handler.server.lock, "read", verb="messages", unlocked=True):
        payload = timing.measured(part="query_ms")(read_messages)(handler.server, principal, query, committed=True)
    handler._json(200, payload)

#: Simple read verbs: ``(store, principal, project) -> data``; the text formatter
#: is ``fmt.<name>_text``.
READ_CALLS = {
    "aliases": lambda store, who, project: store.aliases(who),
    "whoami": lambda store, who, project: store.whoami(who),
    "needs_you": lambda store, who, project: store.needs_you(who, project=project),
    "answers": lambda store, who, project: store.answers(who, project=project),
    "progress": lambda store, who, project: store.progress(who, project=project),
    "jev_report": lambda store, who, project: store.jev_report(who, project=project),
    "apply_grants": lambda store, who, project: {"grants": store.apply_grants(who)},
}

#: M2B: the only verbs a run token may even reach (the store narrows per role).
RUN_VERBS = frozenset({"heartbeat", "add_evidence", "submit", "verdict", "replay", "write_receipt", "write_context", "bind_seat_verdict"})

# These verbs persist their response inside the same transaction as the mutation.
RETRYABLE_WRITES = frozenset({"claim", "claim_batch", "heartbeat", "heartbeat_batch", "set_priority"})


def check_write_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", key):
        raise Refused("validation", "invalid idempotency key")

#: Read-only verbs reachable by the ``sweep`` role (claims, runs, PR links).
SWEEP_READ_VERBS = frozenset({"whoami", "snapshot", "show"})


def check_sweep(principal, action):
    """Enforce the read-only ``sweep`` role at transport and store boundaries."""
    if getattr(principal, "role", None) != "sweep":
        return
    if action not in SWEEP_READ_VERBS:
        raise Refused("forbidden", "sweep role may only read claims, runs, and PR links")
    if action != "whoami" and list(getattr(principal, "projects", None) or []) != []:
        raise Refused("forbidden", "sweep role requires unscoped projects")


def run_gate(principal, name):
    """Refuse host, sweep, phase-worker, and run tokens outside their verbs (403)."""
    from .store_host_samples import HostPrincipal
    if isinstance(principal, HostPrincipal):
        if name != 'ingest_host_sample':
            raise Refused('forbidden', 'host token is ingest-only')
        return
    check_sweep(principal, name)
    from .worker_policy import check
    check(principal, name, transport=True)
    if getattr(principal, "token_id", None) is not None and name not in RUN_VERBS:
        raise Refused("forbidden", "a run token may not call this verb")


STATUS = {"forbidden": 403, "not_found": 404, "conflict": 409, "locked": 409,
          "merge_index_expired": 409, "merge_index_running": 409, "merge_index_failed": 409,
          "epoch_stale": 409, "round_stale": 409, "lease_held": 409, "same_principal": 409,
          "same_family": 409, "unavailable": 503, "exec_stale": 409,
          "bad_transition": 409, "exec_stale_state": 409,
          # R2a': bus codes registered statically (never at runtime);
          # msg_rate_limited is the bus's rate-limit code, distinct from
          # oversight's 403 "forbidden" (R3 keeps no rate_limited enum).
          "bad_signature": 401, "unknown_kid": 401, "skew": 401,
          "replayed_nonce": 401, "replay_window_full": 503,
          "too_large": 413, "msg_rate_limited": 429}


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


# Store-authorized claimant/router proof close (D-5053).
def _close_on_decision(store, principal, body, need):
    if body.get("batch_ref") is None and body.get("decision_ref") is None:
        raise Refused("validation", "one of the arguments --decision-ref --batch is required")
    if body.get("batch_ref") is not None and body.get("epoch") is not None:
        raise Refused("validation", "--epoch requires --decision-ref, not --batch")
    return store.close_on_decision(principal, need(body, "key"),
                                   body.get("batch_ref"),
                                   decision_ref=body.get("decision_ref"),
                                   epoch=body.get("epoch"))


# Router-only verbs: ``need`` is the handler's required-field reader.
def _withdraw_review(store, principal, body, need):
    return store.withdraw_review(principal, body.get("key"), body.get("decision_id"))  # role first (403)


def _reopen(store, principal, body, need):
    return store.reopen(principal, body.get("key"), body.get("reason"),
                        body.get("decision_id"))   # role first (403)


def _set_legacy_state(store, principal, body, need):
    return store.set_legacy_state(principal, need(body, "key"),
                                  need(body, "state"), body.get("note", ""))


def _set_kind(store, principal, body, need):
    extra = set(body) - {"key", "kind"}
    if extra and getattr(principal, "role", None) == "router":   # a non-router gets 403 first
        raise Refused("validation", f"unexpected fields {sorted(extra)}")
    return store.set_kind(principal, need(body, "key"), body.get("kind"))   # role first (403)


def _grant_verb(name):
    def call(store, principal, body, need):
        store._require_role(principal, "router")      # a non-router gets 403 first
        return getattr(store, name)(principal, need(body, "principal"),
                                    need(body, "project"))
    return call


def _apply_ops(store, principal, body, need):
    return store.apply_ops(principal, need(body, "ops"), body.get("batch_ref"),
                           base_seq=body.get("base_seq"),
                           dry_run=body.get("dry_run"),
                           batch_name=body.get("batch"))


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


def _set_metrics(store, principal, body, need):
    return store.set_metrics(principal, body.get("day"), body.get("items"))   # role first (403)


def _governor_override(store, principal, body, need):
    return store.log_governor_override(principal, body.get("start_kind"),
                                       body.get("initiator"), body.get("reason"),
                                       body.get("provider"))   # role first (403)


def _replay(store, principal, body, need):
    return store.replay(principal, body.get("entries"))   # M2B-OUTBOX: principal checked first (403)


def _phase_plan(store, principal, body, need):
    from .worker_policy import check
    check(principal, "phase_plan", transport=True)
    # Role first: a non-router/system principal gets 403 before env or key checks.
    store._require_role(principal, "router", "system", "phase-worker")
    from ..planner.phase import plan_phase   # local import: keeps keeper leaf-free
    key = need(body, "key")
    repo_root = os.environ.get("PG_PHASE_REPO_ROOT")
    from .worker_policy import check_project
    check_project(store, principal, key)
    if not repo_root or not os.path.isdir(repo_root):
        raise Refused("unavailable", "PG_PHASE_REPO_ROOT is not configured")
    # repo_root is server-side only; plan_phase also re-checks the role first.
    return plan_phase(store, principal, key, repo_root=repo_root, now=None)

#: Seam for tests: the keeper's own GitHub adapter, built from the server environment only.
def _github_from_env():
    from ..planner.github_adapter import RestGitHub   # local import: keeps keeper leaf-free
    return RestGitHub.from_env()

def _phase_finish(store, principal, body, need):
    from .worker_policy import check
    check(principal, "phase_finish", transport=True)
    # Role first: a non-router/system principal gets 403 before env, key or GitHub access checks.
    store._require_role(principal, "router", "system", "phase-worker")
    from ..planner.phase_finish import finish_phase
    key, branch, head = need(body, "key"), need(body, "branch"), need(body, "head")
    from .worker_policy import check_project
    check_project(store, principal, key)
    repo_root = os.environ.get("PG_PHASE_REPO_ROOT")
    if not repo_root or not os.path.isdir(repo_root):
        raise Refused("unavailable", "PG_PHASE_REPO_ROOT is not configured")
    github = _github_from_env()
    if github is None:
        raise Refused("unavailable", "phase_finish GitHub access is not configured")
    # repo_root, the repo slug and the token file come from the server environment only, never the body.
    return finish_phase(store, principal, key, branch, head, repo_root=repo_root,
                        github=github, pre_patch_phase=body.get("pre_patch_phase"))

def _worker_claim(store, principal, body, need):
    # Role first: a non-worker principal gets 403 before key or env access checks.
    # The phase runner's system principal also claims cards, so this is not worker-only.
    store._require_role(principal, "worker", "system", "phase-worker")
    from .worker_policy import check_project
    check_project(store, principal, need(body, "card"))
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


ROUTER_VERBS = {"mint_run_token": _mint_run_token,
                "close_merged": lambda store, principal, body, need: store.close_merged(
                    principal, body.get("key"), body.get("decision_id"),
                    # Omission requests recovery; explicit JSON null is malformed.
                    body.get("families") if body.get("families", ()) is not None else (),
                    from_open=body.get("from_open"), apply=body.get("apply", False),
                    waive_seats=body.get("waive_seats"), dry_run=body.get("dry_run", False),
                    require_seats=body.get("require_seats")),
                "replay": _replay,
                "withdraw_review": _withdraw_review,
                "reopen": _reopen,
                "set_kind": _set_kind,
                "requeue": lambda store, principal, body, need: store.requeue(
                    principal, need(body, "key"), body.get("reason", "")),
                "answer": _answer,
                "rehome": _rehome,
                "migrate_descriptions": _migrate_descriptions,
                "set_legacy_state": _set_legacy_state,
                "apply_ops": _apply_ops,
                "grant_apply": _grant_verb("grant_apply"),
                "revoke_apply": _grant_verb("revoke_apply"),
                "record_run": _record_run}


class MergeRefresh:
    """Publish a complete scan to HTTP close calls, including partial-scan errors.

    States: ``idle``, ``running``, ``ready`` and ``failed``. A scan that outlives
    SCAN_BUDGET_SECONDS ends ``failed``: nothing is published and ``consume`` refuses
    until a new ``start`` (or ``resume``) releases it.
    """

    MAX_AGE_SECONDS = 300
    # A scan that ends within this budget leaves MAX_AGE_SECONDS - SCAN_BUDGET_SECONDS (120 s) for
    # the paged close inside the index age; the client waits less (WAIT) so a slow scan is
    # continued by the next scheduled pass instead of failing the unit.
    SCAN_BUDGET_SECONDS = 180
    _FIELDS = ("_warmed", "_lanes", "_deploy_warm", "_merge_scan_errors", "_landings",
               "_named_landings", "_landing_decisions")

    def __init__(self, server):
        self.server = server
        self.state = "idle"
        self.used = False
        self.expires_at = 0.0
        self._lock = threading.Lock()
        self._generation = 0
        self._bundle = None

    def control(self, action):
        server = self.server
        if action == "projects":
            with timing.outside_request_lock(server.lock) if getattr(server, "read_path", None) else nullcontext():
                candidates, _ = self._read_candidates()
            projects = sorted({project for _, project in candidates})
            with self._lock:
                if not projects and self.state == "ready":
                    self.used = True
            return {"projects": projects}
        if action not in ("start", "status", "resume"):
            raise Refused("validation", "refresh must be start, resume, status or projects")
        with self._lock:
            return self._control(action)

    def _control(self, action):
        if action != "status" and self.state != "running":
            now = monotonic()
            fresh = self.state == "ready" and now < self.expires_at
            # resume continues a scheduled pass: a completed scan another caller already consumed
            # is reused while it is fresh; start always replaces a used scan.
            if self.state in ("idle", "failed") or not fresh or (self.used and action == "start"):
                # Bound reuse from the beginning of the scan, when it starts fetching default refs.
                self.expires_at = now + self.MAX_AGE_SECONDS
                self.state, self.used = "running", False
                self._generation += 1
                threading.Thread(target=self._build, args=(self._generation, now),
                                 name="keeper-merge-index", daemon=True).start()
        return {"scan": self.state}

    def consume(self):
        with self._lock:
            self._consume()

    def _consume(self):
        if self.state == "running":
            raise Refused("merge_index_running", "merge index refresh is running")
        if self.state == "failed":
            raise Refused("merge_index_failed", "merge index refresh failed; start a refresh")
        if self.state == "ready":
            if monotonic() >= self.expires_at:
                raise Refused("merge_index_expired", "merge index expired; start a refresh")
            self.used = True
            if self._bundle is not None:
                self.server.store.__dict__.update(self._bundle)
                self._bundle = None

    def _read_candidates(self):
        server = self.server
        if getattr(server, "read_path", None):
            with server.read_store(defer_audit=True) as scan:
                candidates = scan.merge_candidates()
        else:
            with server.lock:
                scan = copy(server.store)
                candidates = scan.merge_candidates()
        scan.conn = None
        scan.__dict__.pop("_frozen_lanes", None)
        return candidates, scan

    def _build(self, generation, started):
        deadline = started + self.SCAN_BUDGET_SECONDS
        with timing.io_budget(max(0, deadline - monotonic())):
            try:
                candidates, scan = self._read_candidates()
                # Warm reuses these inputs. Take owned copies so a failed scan cannot leak changes.
                for field in self._FIELDS:
                    value = scan.__dict__.get(field)
                    if isinstance(value, dict):
                        scan.__dict__[field] = deepcopy(value.copy())
                scan.warm_merge_scan(candidates)
                published = {field: scan.__dict__[field] for field in self._FIELDS}
            except Exception:
                published = None
            with self._lock:
                if generation != self._generation:
                    return
                over = monotonic() >= deadline or timing.io_timeout(1) <= 0
                self._bundle = None if over else published
                self.state = "ready" if self._bundle is not None else "failed"
        log = logging.getLogger("plangraph.keeper")
        seconds = monotonic() - started
        if over or published is None:
            log.warning("merge_index_refresh failed %s seconds=%.1f",
                        "budget_exceeded" if over else "scan_error", seconds)
        else:
            log.info("merge_index_refresh repos=%d failed=%d seconds=%.1f",
                     len(published["_warmed"]), len(published["_merge_scan_errors"]), seconds)


def _close_merged_auto(store, principal, body, need):
    watch, apply = body.get("watch", False), body.get("apply", True)
    project, repos_only = body.get("project"), body.get("repos_only", False)
    limit, after = body.get("limit"), body.get("after")
    if type(watch) is not bool or watch and (
            apply is not False or repos_only or project is not None or limit is not None or after is not None):
        raise Refused("validation", "watch requires a complete dry run across all repositories")
    if not watch and limit is None:
        limit = 25
    with timing.io_budget(90):
        report = store.close_merged_auto(
            principal, project, require_seats=body.get("require_seats"),
            apply=apply, repos_only=repos_only, frozen=True,
            limit=limit, after=after)
        if watch:
            if report["scan_errors"] or report["next_after"] is not None:
                raise Refused("evidence_unverified", "merged-open watcher scan incomplete")
            store._add_decision_reconcile_rows(report)
            report["alert"] = store._watch_merged_open(report)
        return report


#: Write verbs whose store method checks the role itself and admits more than the
#: router (set_metrics, close_merged_auto: router, system; governor_override: router, l1, system, spawner), so they are not in ROUTER_VERBS.
ROLE_CHECKED_VERBS = {"set_metrics": _set_metrics,
                      "land": lambda store, principal, body, need: store.land(principal, body),
                      "record_spawn_attempt": lambda store, principal, body, need:
                          store.record_spawn_attempt(principal, body),
                      "close_merged_auto": _close_merged_auto,
                      "close_on_decision": _close_on_decision,
                      "governor_override": _governor_override,
                      "phase_plan": _phase_plan,
                      "phase_finish": _phase_finish,
                      "worker_claim": _worker_claim}
