"""The keeper Store: shared plumbing, transactions, audit and verb helpers.

``Store`` is the only object that mutates keeper state.  Every verb takes an
already-authenticated :class:`Principal`, authorises it, validates the request,
runs one transaction, and writes exactly one ``audit_log`` row — including for
refusals, which commit only that audit row.  Verbs are grouped by
responsibility: ``store_create`` (definitions), ``store_verbs`` (the lifecycle)
and ``store_read`` (views).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import views
from . import events, evidence
from .errors import Refused
from .evidence_types import Context
from .store_create import CreateMixin
from .store_read import ReadMixin
from .store_verbs import VerbsMixin

__all__ = ["Store", "Refused", "iso", "parse_iso"]

DEFAULT_LEASE_S = 7200
REVIEW_OVERDUE_S = 4 * 3600
LEASE_WARN_S = 15 * 60


def iso(ts) -> str:
    """SECONDS-since-epoch -> the snapshot ISO-8601 form (``...Z``)."""
    moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    """Parse an ISO timestamp; ``None`` for empty/invalid input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _json_list(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or [])
    except (ValueError, TypeError):
        return []
    return list(parsed) if isinstance(parsed, (list, tuple)) else []


def _json_dict(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or {})
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Store(CreateMixin, VerbsMixin, ReadMixin):
    """State machine, locking and audit over one keeper connection."""

    def __init__(self, conn, clock=None, verifiers=None, ctx=None):
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        # Default to the real registry; tests inject fakes.  ``ctx`` supplies the
        # adapters the verifiers read through (``default_context`` in production).
        self.verifiers = dict(evidence.VERIFIERS if verifiers is None else verifiers)
        self.ctx = ctx
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

    def _ctx(self, item, now):
        """The verifier context for *item* at *now*.

        The injected ``ctx`` supplies the adapters; the store always overrides
        ``now`` and derives ``claim_time`` from the item's latest ``claimed``
        event, so the real verifiers compare a commit against the actual claim
        instant rather than a caller-supplied one.
        """
        base = self.ctx

        def adapter(name):
            if base is None:
                return None
            if isinstance(base, dict):
                return base.get(name)
            return getattr(base, name, None)

        return Context(
            git=adapter("git"), ci=adapter("ci"), runs=adapter("runs"),
            semaphore=adapter("semaphore"), decisions=adapter("decisions"),
            now=now, claim_time=self._claim_time(item))

    def _claim_time(self, item):
        """The epoch of the item's current claim, or ``None`` if never claimed."""
        row = self.conn.execute(
            "SELECT ts FROM events WHERE key=? AND type='claimed'"
            " ORDER BY seq DESC LIMIT 1", (item["key"],)).fetchone()
        if row is None:
            return None
        parsed = parse_iso(row["ts"])
        return parsed.timestamp() if parsed is not None else None

    # ------------------------------------------------------- transactions
    @contextmanager
    def _write(self):
        """One immediate write transaction; roll back on any failure."""
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

    def _run(self, principal, action, key, fn):
        """Authorise/execute *fn*; audit success and every refusal."""
        now = self.now()
        try:
            result = fn(now)
        except Refused as exc:
            self._audit(principal, action, key, False, exc.code, exc.msg, now)
            raise
        self._audit(principal, action, key, True, "", "", now)
        return result

    # -------------------------------------------------------------- reads
    def _item(self, key):
        row = self.conn.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        item = self._hydrate(dict(row))
        item["depends_on"] = self._dep_map().get(key, [])
        return item

    @staticmethod
    def _hydrate(item):
        """Decode the JSON/bool columns of a raw items row."""
        item["evidence_kinds"] = _json_list(item.get("evidence_kinds"))
        item["operator"] = bool(item.get("operator"))
        return item

    def _dep_map(self):
        deps = {}
        for row in self.conn.execute("SELECT src,dst FROM edges"):
            deps.setdefault(row["src"], []).append(row["dst"])
        return {k: sorted(v) for k, v in deps.items()}

    def _all_items(self):
        """Every item in the views/SPEC item shape (with deps resolved)."""
        deps = self._dep_map()
        out = []
        for row in self.conn.execute("SELECT * FROM items ORDER BY key"):
            item = self._hydrate(dict(row))
            item["depends_on"] = deps.get(item["key"], [])
            out.append(item)
        return out

    def _evidence(self, key):
        rows = self.conn.execute(
            "SELECT id,key,epoch,kind,ref,verified,detail,at FROM evidence"
            " WHERE key=? ORDER BY id", (key,)).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["ref"] = _json_dict(record.get("ref"))
            record["verified"] = bool(record["verified"])
            out.append(record)
        return out

    # ------------------------------------------------------ verification
    def verify_chain(self):
        return events.verify_chain(self.conn)

    def verify_integrity(self):
        return events.verify_integrity(self.conn)

    def _append(self, principal, key, type, payload, now):
        return events.append_event(
            self.conn, iso(now), getattr(principal, "name", None),
            getattr(principal, "role", None), None, key, type, payload)

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

    def _get_or_refuse(self, key):
        item = self._item(key)
        if item is None:
            raise Refused("not_found", f"no item {key!r}")
        return item

    def _assert_unlocked(self, item):
        if item["state"] == "cancelled":
            raise Refused("locked", f"{item['key']} is cancelled")
        reviewed = self.conn.execute(
            "SELECT 1 FROM events WHERE key=? AND type='review_passed' LIMIT 1",
            (item["key"],)).fetchone()
        if reviewed is not None:
            raise Refused("locked", f"{item['key']} is DONE")

    # ------------------------------------------------- shared verb checks
    def _actor_or_delegate(self, principal, item):
        if principal.name not in (item["claimed_by"], item["delegate"]):
            raise Refused("forbidden", "only the claimant or delegate may do this")

    def _epoch_ok(self, item, epoch):
        if item["epoch"] != int(epoch):
            raise Refused("epoch_stale",
                          f"epoch {epoch} != current {item['epoch']}")

    def _ready(self, item):
        for dep in self._dep_map().get(item["key"], []):
            target = self._item(dep)
            if target is None or target["state"] != "done":
                return False
        return True

    def _validate(self, items):
        problems = views.validate(items)
        if not problems:
            return
        cycle = [p for p in problems if p["code"] == "cycle"]
        if cycle:
            raise Refused("cycle", cycle[0]["msg"])
        detail = "; ".join(f"{p['code']}:{p['key']}:{p['msg']}" for p in problems)
        raise Refused("validation", detail)

    def _normalise(self, raw):
        project = raw.get("project")
        identifier = raw.get("id")
        if not project or not identifier:
            raise Refused("validation", "each item needs project and id")
        key = f"{project}:{identifier}"
        if raw.get("key") and raw["key"] != key:
            raise Refused("validation", f"key {raw['key']!r} != {key!r}")
        deps = [self._qualify(project, dep) for dep in raw.get("depends_on") or []]
        corrects = raw.get("corrects")
        if corrects:
            corrects = self._qualify(project, corrects)
        return {
            "key": key, "project": project, "id": identifier,
            "title": raw.get("title", ""), "kind": raw.get("kind", "lane"),
            "track": raw.get("track"), "owner": raw.get("owner"),
            "estimate_h": raw.get("estimate_h"), "estimate_src": raw.get("estimate_src"),
            "done_criteria": raw.get("done_criteria"),
            "evidence_kinds": list(raw.get("evidence_kinds") or ["any"]),
            "operator": bool(raw.get("operator")),
            "priority": int(raw.get("priority") or 0),
            "source": raw.get("source"), "note": raw.get("note"),
            "corrects": corrects, "depends_on": deps,
            "state": raw.get("state", "open"),
        }

    @staticmethod
    def _qualify(project, dep):
        return dep if ":" in dep else f"{project}:{dep}"

    @staticmethod
    def _stored(field, value):
        if field == "evidence_kinds":
            return json.dumps(list(value))
        if field == "operator":
            return 1 if value else 0
        return value

    def _require_evidence(self, item):
        kinds = item["evidence_kinds"] or ["any"]
        evidence = [e for e in self._evidence(item["key"])
                    if e["epoch"] == item["epoch"]]
        if kinds == ["any"]:
            if not evidence:
                raise Refused("evidence_missing", "no evidence attached")
            if not any(e["verified"] for e in evidence):
                raise Refused("evidence_unverified", "no verified evidence")
            return
        missing = [k for k in kinds if not any(e["kind"] == k for e in evidence)]
        if missing:
            raise Refused("evidence_missing", f"missing evidence: {missing}")
        unverified = [k for k in kinds
                      if not any(e["kind"] == k and e["verified"]
                                 for e in evidence)]
        if unverified:
            raise Refused("evidence_unverified", f"unverified evidence: {unverified}")

    def _reviewer_family_ok(self, principal, item):
        from . import auth
        claimer = auth.get_principal(self.conn, item["claimed_by"])
        delegate = (auth.get_principal(self.conn, item["delegate"])
                    if item["delegate"] else None)
        peer = delegate or claimer
        if principal.family is None or peer is None or peer.family is None:
            raise Refused("family_unknown", "missing family; refusing to review")
        if principal.family == peer.family:
            raise Refused("same_family", "reviewer shares the claimant's family")

    def _reverify_all(self, item, now):
        evidence = self._evidence(item["key"])
        if not evidence:
            raise Refused("evidence_missing", "no evidence to verify")
        for record in evidence:
            verifier = self.verifiers.get(record["kind"])
            if verifier is None:
                raise Refused("evidence_unverified",
                              f"no verifier for {record['kind']!r}")
            result = verifier.verify(item, record["ref"], self._ctx(item, now))
            if not result.ok:
                raise Refused("evidence_unverified",
                              result.detail or "stale evidence")

    def _has_finding(self, key, code):
        return self.conn.execute(
            "SELECT 1 FROM audit_findings WHERE key=? AND code=? LIMIT 1",
            (key, code)).fetchone() is not None
