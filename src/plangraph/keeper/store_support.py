"""Shared plumbing for :class:`plangraph.keeper.store.Store`.

None of these are verbs: they are the row hydration helpers, the timestamp
codec, the event append and the evidence-verification helpers that every verb
group reuses.  They live on a mixin so ``Store`` keeps one public API
(``SupportMixin`` is first in its MRO) while ``store.py`` stays a readable
transaction/authorisation core.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import views
from . import events
from .errors import Refused
from .evidence_types import Context

__all__ = ["SupportMixin", "iso", "parse_iso"]


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


class SupportMixin:
    """Row hydration, event append and evidence-verification helpers."""

    # ------------------------------------------------------- verification
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

    def verify_chain(self):
        return events.verify_chain(self.conn)

    def verify_integrity(self):
        return events.verify_integrity(self.conn)

    def _append(self, principal, key, type, payload, now):
        return events.append_event(
            self.conn, iso(now), getattr(principal, "name", None),
            getattr(principal, "role", None), None, key, type, payload)

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

    def _verified_evidence_ids(self, item):
        """Row ids of the current epoch's *verified* evidence."""
        return frozenset(
            e["id"] for e in self._evidence(item["key"])
            if e["epoch"] == item["epoch"] and e["verified"])

    # -------------------------------------------------- evidence rules
    def _verify_evidence(self, item, kind, ref, now):
        """Run the verifier for *kind*; the slow I/O a verb does pre-txn."""
        verifier = self.verifiers.get(kind)
        if verifier is None:
            raise Refused("validation", f"unknown evidence kind {kind!r}")
        return verifier.verify(item, ref or {}, self._ctx(item, now))

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
        """Refuse unless the reviewer's family differs from both peers.

        A legacy import claims by an ``owner`` string that need not be a
        principal; its ``owner_family`` (set by the router at import time) then
        stands in for the claimant's family.  Either peer with an unknown family
        fails closed, as does a reviewer with none.
        """
        from . import auth
        if principal.family is None:
            raise Refused("family_unknown", "reviewer has no family")
        peers = []
        claimer = auth.get_principal(self.conn, item["claimed_by"])
        claimant_family = (claimer.family if claimer is not None
                           else item.get("owner_family"))
        if claimant_family is None:
            raise Refused("family_unknown", "claimant family unknown")
        peers.append(claimant_family)
        if item["delegate"]:
            delegate = auth.get_principal(self.conn, item["delegate"])
            if delegate is None or delegate.family is None:
                raise Refused("family_unknown", "delegate family unknown")
            peers.append(delegate.family)
        if principal.family in peers:
            raise Refused("same_family", "reviewer shares a family with the item")

    def _reverify_all(self, item, now):
        """Re-run the verifiers for the current epoch's *verified* evidence.

        Only rows already recorded as ``verified`` are re-checked: an unverified
        row never contributed to the submit requirement, so it cannot make a
        verdict fail, and evidence from a released epoch is irrelevant.
        """
        evidence = [e for e in self._evidence(item["key"])
                    if e["epoch"] == item["epoch"] and e["verified"]]
        if not evidence:
            raise Refused("evidence_missing", "no verified evidence to verify")
        for record in evidence:
            verifier = self.verifiers.get(record["kind"])
            if verifier is None:
                raise Refused("evidence_unverified",
                              f"no verifier for {record['kind']!r}")
            result = verifier.verify(item, record["ref"], self._ctx(item, now))
            if not result.ok:
                raise Refused("evidence_unverified",
                              result.detail or "stale evidence")
        return frozenset(e["id"] for e in evidence)

    def _race_guard(self, cursor, key, expected_state, expected_epoch):
        """Refuse unless a guarded UPDATE hit exactly one row.

        Unlike ``Store._row_guard`` (kept for the verbs that already shipped
        it), an epoch change here is reported as ``epoch_stale`` and a
        same-epoch state change as ``conflict`` -- the two codes a caller of a
        race-hardened verb expects.
        """
        if cursor.rowcount == 1:
            return
        fresh = self._item(key)
        if fresh is None:
            raise Refused("not_found", f"no item {key!r}")
        if int(fresh["epoch"]) != int(expected_epoch):
            raise Refused("epoch_stale",
                          f"epoch {expected_epoch} != current {fresh['epoch']}")
        raise Refused("conflict",
                      f"{key} is {fresh['state']}, not {expected_state}")

    def _guarded_touch(self, key, states, epoch):
        """A no-op guarded UPDATE used by verbs that only INSERT.

        ``add_evidence`` never updates the item row, but it still must prove
        the row is in one of *states* at *epoch* before inserting.  The no-op
        update yields a rowcount the shared guard can check.
        """
        placeholders = ",".join("?" for _ in states)
        cursor = self.conn.execute(
            "UPDATE items SET updated_at=updated_at WHERE key=? AND epoch=?"
            f" AND state IN ({placeholders})",
            (key, int(epoch), *states))
        self._race_guard(cursor, key, states[0], epoch)
        return cursor

    @staticmethod
    def _changed(items, key, state):
        """A shallow copy of *items* with *key*'s state set to *state*."""
        out = []
        for item in items:
            copy = dict(item)
            if copy["key"] == key:
                copy["state"] = state
            out.append(copy)
        return out

    def _validate_change(self, after, before, affected=(), allow_new=()):
        """Validate a proposed graph, refusing new problems and touched ones.

        ``views.validate`` reports whole-graph problems.  A pre-existing
        problem elsewhere must not block an unrelated write; it is refused only
        when it touches a changed key.  On top of that, a problem that is
        *new* compared with ``before`` is refused even when its key was not
        changed -- that is how a reachability regression (dropping a
        dependency leaving a pre-existing item unreachable) is caught.
        ``allow_new`` names codes a verb is expected to introduce (``cancel``
        deliberately creates ``depends_on_cancelled`` for its dependents).
        """
        baseline = {(p["code"], p["key"]) for p in views.validate(before)}
        problems = []
        for problem in views.validate(after):
            token = (problem["code"], problem["key"])
            if token in baseline:
                if self._problem_affects(problem, affected):
                    problems.append(problem)
            elif problem["code"] not in allow_new:
                problems.append(problem)
        if not problems:
            return
        cycle = [p for p in problems if p["code"] == "cycle"]
        if cycle:
            raise Refused("cycle", cycle[0]["msg"])
        detail = "; ".join(
            f"{p['code']}:{p['key']}:{p['msg']}" for p in problems)
        raise Refused("validation", detail)

    def _has_finding(self, key, code):
        return self.conn.execute(
            "SELECT 1 FROM audit_findings WHERE key=? AND code=? LIMIT 1",
            (key, code)).fetchone() is not None
