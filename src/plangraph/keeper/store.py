"""The keeper Store: authorisation, transactions, audit and the verb helpers.

``Store`` is the only object that mutates keeper state.  Every verb takes an
already-authenticated :class:`Principal`, authorises it, validates the request,
runs one transaction, and writes exactly one ``audit_log`` row — including for
refusals, which commit only that audit row.  Verbs are grouped by
responsibility: ``store_create`` (definitions), ``store_verbs`` (the lifecycle),
``store_reap`` (the reaper) and ``store_read`` (views); the shared plumbing —
row hydration, the timestamp codec, event append and the evidence checks —
lives in ``store_support``.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import views
from . import auth, evidence
from .errors import Refused
from .store_answer import AnswerMixin
from .store_close import CloseMixin
from .store_create import CreateMixin
from .store_legacy import LegacyMixin
from .store_input import InputMixin
from .store_names import NamesMixin
from .store_needs import NeedsMixin
from .store_ops import OpsMixin
from .store_read import ReadMixin
from .store_rehome import RehomeMixin
from .store_reap import ReapMixin
from .store_review import ReviewMixin
from .store_runs import RunsMixin
from .store_support import SupportMixin, iso, parse_iso
from .store_verbs import VerbsMixin
from .store_waiver import WaiverMixin

__all__ = ["Store", "Refused", "iso", "parse_iso"]

DEFAULT_LEASE_S = 7200
REVIEW_OVERDUE_S = 4 * 3600
LEASE_WARN_S = 15 * 60

_LEASE_MIN, _LEASE_MAX = 60, 86400

# Exceptions raised by a verb that mean the request was malformed, not that the
# server crashed.  ``_run`` turns them into an audited ``Refused``.
_MALFORMED = (TypeError, ValueError, AttributeError, KeyError,
              sqlite3.ProgrammingError)

#: X7b: evidence kinds a claimant may never attach for itself.  Only the router
#: puts legacy proof on an item, and only via ``add_evidence_router``.
ROUTER_ONLY_EVIDENCE_KINDS = frozenset({"commit"})


class Store(SupportMixin, InputMixin, CreateMixin, VerbsMixin, ReviewMixin,
            ReapMixin, ReadMixin, WaiverMixin, CloseMixin, LegacyMixin, OpsMixin,
            NamesMixin, RehomeMixin, RunsMixin, AnswerMixin, NeedsMixin):
    """State machine, locking and audit over one keeper connection."""

    def __init__(self, conn, clock=None, verifiers=None, ctx=None,
                 batch_roots=None):
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        # Default to the real registry; tests inject fakes.  ``ctx`` supplies the
        # adapters the verifiers read through (``default_context`` in production).
        self.verifiers = dict(evidence.VERIFIERS if verifiers is None else verifiers)
        self.ctx = ctx
        # X7b: the directories ``close_on_decision`` may read a batch file from.
        # Empty by default, so the verb fails closed until the operator opts in.
        self.batch_roots = [str(r) for r in (batch_roots or [])]
        # B1: the OpBatch of an ``apply_ops`` in flight (``None`` otherwise).
        self._batch = None
        self.default_lease = DEFAULT_LEASE_S
        self.review_overdue_s = REVIEW_OVERDUE_S
        self.lease_warn_s = LEASE_WARN_S

    # ------------------------------------------------------------ time
    def now(self):
        return float(self.clock())

    @staticmethod
    def _iso(ts):
        return iso(ts)

    @staticmethod
    def _parse_iso(value):
        return parse_iso(value)

    # ------------------------------------------------------- transactions
    @contextmanager
    def _write(self):
        """One immediate write transaction; roll back on any failure.

        Inside ``apply_ops`` the batch already holds the transaction, so a
        single verb's own write block just runs in it.
        """
        if self._batch is not None:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -------------------------------------------------------------- audit
    def _audit(self, principal, action, key, ok, code="", msg="", now=None):
        self.conn.execute(
            "INSERT INTO audit_log(ts,actor,role,action,key,ok,code,msg)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (iso(now if now is not None else self.now()),
             getattr(principal, "name", None), getattr(principal, "role", None),
             action, key, 1 if ok else 0, code or None, msg or None))

    def _finding(self, key, code, msg, now):
        self.conn.execute(
            "INSERT INTO audit_findings(ts,key,code,msg) VALUES(?,?,?,?)",
            (iso(now), key, code, msg))

    def audit_auth_failure(self, reason="", now=None):
        """Record a rejected authentication by reason only, never a token.

        The actor is ``?`` because no principal was resolved, and the message is
        capped so a caller cannot use a failure reason to bloat the audit log.
        """
        moment = now if now is not None else self.now()
        msg = str(reason or "")[:200]
        self.conn.execute(
            "INSERT INTO audit_log(ts,actor,role,action,key,ok,code,msg)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (iso(moment), "?", None, "authenticate", None, 0, "forbidden", msg))

    def _check_principal(self, principal):
        """Refuse a principal whose stored row was revoked or changed.

        Re-read at the top of every verb so a token that was valid at
        authentication cannot keep acting after a revoke or a role/project edit.
        A principal with no row is refused too, so a stale or forged identity
        cannot act; the sole exception is the API reaper's synthetic
        ``system``/``system`` identity, which has no row of its own.
        """
        row = self.conn.execute(
            "SELECT role,projects,active FROM principals WHERE name=?",
            (getattr(principal, "name", None),)).fetchone()
        if row is None:
            if (getattr(principal, "name", None) == "system"
                    and getattr(principal, "role", None) == "system"):
                return
            raise Refused("forbidden", "principal has no stored identity")
        if not row["active"]:
            raise Refused("forbidden", "principal is revoked or inactive")
        try:
            projects = json.loads(row["projects"] or "[]")
        except (ValueError, TypeError):
            projects = []
        if row["role"] != getattr(principal, "role", None) \
                or projects != list(getattr(principal, "projects", None) or []):
            raise Refused("forbidden", "principal changed since authentication")

    def _run(self, principal, action, key, fn):
        """Authorise/execute *fn*; audit success and every refusal."""
        now = self.now()
        try:
            self._check_principal(principal)
            result = fn(now)
        except Refused as exc:
            self._audit(principal, action, key, False, exc.code, exc.msg, now)
            raise
        except sqlite3.IntegrityError as exc:
            # A uniqueness/foreign-key violation is a lost race or duplicate.
            self._audit(principal, action, key, False, "conflict", str(exc), now)
            raise Refused("conflict",
                          "keeper data conflicts with this request") from exc
        except _MALFORMED:
            # A malformed request (bad body shape, wrong parameter type) is a
            # validation refusal, not a crash.  ``_write`` has already rolled
            # back any partial transaction before we get here.  Log the
            # traceback for the operator, but name only the verb and key: the
            # request body may carry a token or other secret and must never
            # reach the log.
            logging.getLogger("plangraph.keeper").exception(
                "malformed input for %s %s", action, key)
            self._audit(principal, action, key, False, "validation",
                        "malformed input", now)
            raise Refused("validation", "malformed input")
        except sqlite3.OperationalError as exc:
            # A concurrent writer holding the write lock is a lost race, not an
            # unhandled crash: report it as a conflict and audit the refusal.
            if "lock" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            self._audit(principal, action, key, False, "conflict", str(exc), now)
            raise Refused("conflict", "keeper database is busy") from exc
        self._audit(principal, action, key, True, "", "", now)
        return result

    # ---------------------------------------------------- authorisation
    @staticmethod
    def _require_role(principal, *roles):
        if principal.role not in roles:
            raise Refused("forbidden", f"role {principal.role!r} may not do this")

    @staticmethod
    def _may_manage(principal, project):
        """Router manages every project; an L1 manages only its own."""
        if principal.role == "router":
            return True
        if principal.role == "l1" and project in (principal.projects or []):
            return True
        raise Refused("forbidden", f"not allowed to manage project {project!r}")

    @staticmethod
    def _require_project(principal, project):
        """Workers and reviewers act only inside a project they are scoped to.

        An empty ``projects`` list means "no project", i.e. nothing is allowed.
        """
        if project not in (principal.projects or []):
            raise Refused("forbidden", f"not scoped to project {project!r}")

    def _get_or_refuse(self, key):
        item = self._item(key)
        if item is None:
            raise Refused("not_found", f"no item {key!r}")
        return item

    def _assert_unlocked(self, item):
        if item["state"] == "cancelled":
            raise Refused("locked", f"{item['key']} is cancelled")
        reviewed = self.conn.execute(
            "SELECT 1 FROM events WHERE key=? AND type IN"
            " ('review_passed','closed_on_decision','answered') LIMIT 1",
            (item["key"],)).fetchone()
        if reviewed is not None:
            raise Refused("locked", f"{item['key']} is DONE")

    # ------------------------------------------------- shared verb checks
    def add_evidence(self, principal, key, epoch, kind, ref):
        """The claimant evidence verb, fenced for the X7b legacy kinds.

        ``commit`` proves only "this sha is on main": no trailer binding, no
        claim-time window, no CI.  Only the router may attach it, and only
        through ``add_evidence_router``, so the claimant path refuses it.
        """
        if kind in ROUTER_ONLY_EVIDENCE_KINDS:
            return self._run(principal, "add_evidence", key,
                             lambda now: self._refuse_router_only_kind(kind))
        return super().add_evidence(principal, key, epoch, kind, ref)

    @staticmethod
    def _refuse_router_only_kind(kind):
        raise Refused("forbidden",
                      f"evidence kind {kind!r} is router-only: use"
                      " add_evidence_router")

    def _actor_or_delegate(self, principal, item):
        if principal.name not in (item["claimed_by"], item["delegate"]):
            raise Refused("forbidden", "only the claimant or delegate may do this")

    def _epoch_ok(self, item, epoch):
        if item["epoch"] != int(epoch):
            raise Refused("epoch_stale",
                          f"epoch {epoch} != current {item['epoch']}")

    def _row_guard(self, cursor, key, expected_state, expected_epoch):
        """Refuse unless the guarded UPDATE hit exactly one expected row.

        A competing writer may have moved the item after our read but before
        the guarded write.  An epoch change is a lost race (``conflict``); a
        same-epoch state change is an ordinary ``bad_state``.
        """
        if cursor.rowcount == 1:
            return
        fresh = self._item(key)
        if fresh is None:
            raise Refused("not_found", f"no item {key!r}")
        if int(fresh["epoch"]) != int(expected_epoch):
            raise Refused("conflict", f"{key} changed under the transaction")
        raise Refused("bad_state",
                      f"{key} is {fresh['state']}, not {expected_state}")

    def _ready(self, item):
        for dep in self._dep_map().get(item["key"], []):
            target = self._item(dep)
            if target is None or target["state"] != "done":
                return False
        return True

    @staticmethod
    def _problem_affects(problem, affected):
        """Does *problem* concern one of the new/changed keys?

        ``views.validate`` reports whole-graph problems; create/edit must only
        be refused for the ones this batch introduces (a pre-existing broken
        neighbour is not the caller's fault).  A cycle is attributed if any node
        on it is new/changed; ``no_goal`` is global, so it concerns any batch.
        """
        if problem["key"] in affected:
            return True
        if problem["code"] == "cycle":
            nodes = problem["msg"].split(":", 1)[-1].replace(" ", "").split("->")
            return any(node in affected for node in nodes)
        return problem["code"] == "no_goal"

    def _validate(self, items, affected=()):
        """Validate the whole graph, refusing only problems touching *affected*."""
        problems = views.validate(items)
        affected = set(affected)
        problems = [p for p in problems if self._problem_affects(p, affected)]
        if not problems:
            return
        cycle = [p for p in problems if p["code"] == "cycle"]
        if cycle:
            raise Refused("cycle", cycle[0]["msg"])
        detail = "; ".join(f"{p['code']}:{p['key']}:{p['msg']}" for p in problems)
        raise Refused("validation", detail)

    def _lease_seconds(self, value):
        """Validate and clamp a requested lease to [60, 86400] seconds."""
        if value is None:
            return float(self.default_lease)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Refused("validation", "lease_s must be a number")
        seconds = float(value)
        if math.isnan(seconds) or math.isinf(seconds):
            raise Refused("validation", "lease_s must be a finite number")
        return min(max(seconds, _LEASE_MIN), _LEASE_MAX)

    @staticmethod
    def _stored(field, value):
        if field == "evidence_kinds":
            return json.dumps(list(value))
        if field == "operator":
            return 1 if value else 0
        return value
