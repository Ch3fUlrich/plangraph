"""Opt-in phase worker scope; provisioning and token selection are operator-only."""
from .auth import PhaseWorkerPrincipal
from .errors import Refused

# Every HTTP call in rust/worker/src/keeper.rs, no administrative/read extras.
VERBS = frozenset({"claim", "worker_claim", "show", "phase_plan", "phase_finish",
                   "attempts", "park", "complete", "alarms", "locks",
                   "ask_question", "answer_question"})
ACTIONS = frozenset({"claim", "show", "worker_attempt", "worker_park",
                     "worker_complete", "worker_alarm", "lock_acquire",
                     "lock_release", "ask_question", "answer_question",
                     "get_card", "attach_merge_evidence"})


def check_identity(principal, row):
    """Require worker scope to match both the identity type and stored role."""
    scoped = getattr(principal, "worker_scope", False)
    stored_worker = row is not None and row["role"] == "phase-worker"
    if scoped or stored_worker or isinstance(principal, PhaseWorkerPrincipal):
        if not scoped or not isinstance(principal, PhaseWorkerPrincipal) \
                or row is None or row["role"] != "phase-worker":
            raise Refused("forbidden", "principal changed since authentication: worker scope requires a stored phase-worker identity")


def check(principal, action, *, transport=False):
    if getattr(principal, "role", None) == "phase-worker" and not getattr(principal, "worker_scope", False):
        raise Refused("forbidden", "phase worker requires authenticated worker scope")
    if getattr(principal, "worker_scope", False):
        if action not in (VERBS if transport else ACTIONS):
            raise Refused("forbidden", "phase worker scope excludes this verb")


def check_lock_project(store, principal, repo):
    if getattr(principal, "worker_scope", False):
        import os

        store._check_principal(principal)
        if isinstance(repo, str) and ":" in repo:
            check_project(store, principal, repo)
            return
        # worker_claim returns the configured root, and Rust forwards it as-is.
        # Resolve scope from the server's cards, never from a caller's path.
        if not repo or repo != os.environ.get("PG_PHASE_REPO_ROOT"):
            raise Refused("forbidden", "worker lock needs a configured project repository")
        rows = store.conn.execute(
            "SELECT DISTINCT i.project FROM items i JOIN cards c ON c.key=i.key"
            " WHERE i.claimed_by=?",
            (principal.name,)).fetchall()
        if not rows:
            raise Refused("forbidden", "worker lock needs a claimed project card")
        for row in rows:
            store._require_project(principal, row["project"])


def check_project(store, principal, key):
    if getattr(principal, "worker_scope", False):
        store._check_principal(principal)
        if key is not None:
            if not isinstance(key, str) or ":" not in key:
                raise Refused("forbidden", "worker key needs a project prefix")
            store._require_project(principal, key.split(":", 1)[0])
