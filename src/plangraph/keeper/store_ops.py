"""B1: the ``apply_ops`` verb for :class:`...store.Store`.

``apply_ops(principal, ops, batch_ref=None, base_seq=None, dry_run=False)``
applies a list of BATCH.md ops (create, edit, legacy_state, add_evidence,
close_on_decision, cancel, priority, rehome, set_kind, add_dep, remove_dep) in
ONE ``BEGIN IMMEDIATE`` transaction, all or nothing.  The router owns the verb;
an ``oversight`` principal may use it for its narrow grant only (see
:mod:`.oversight_policy`), with ``batch_ref`` required.  ``base_seq`` is an
optimistic precondition: a ``conflict`` when any touched key has events newer
than it, checked under the write lock.  ``dry_run`` performs every check and op
and then rolls the transaction back, so a dry run changes no state (an
oversight dry run writes no applied audit rows either; a refusal still records
its denial).  Every op runs the very same
inner function as its single verb (so the same auth/state checks), except that
the whole-graph validation is deferred: the single verbs' ``_validate_change``
calls are recorded while a batch is open, and the validation runs ONCE over the
final graph against the graph as it was when the lock was taken.  Later ops
therefore see the effects of earlier ones (create a leaf, then link it).

Any refusal, validation problem or error rolls back the items, edges, events
and evidence of the whole batch; the refusal names the op:
``op <n> <op> <key>: <code> <message>`` (``n`` is 1-based, like ``apply_batch.py``).

Slow I/O runs before the lock: evidence for a ``claimed``/``in_review`` item
that no earlier op of the batch moves is verified pre-lock, and the in-lock op
re-checks that item's state and epoch (else ``conflict``).  Evidence for an
item an earlier op creates or moves cannot exist pre-lock, so it is verified
in-lock; ``close_on_decision`` re-reads its (small) batch and decision files
in-lock exactly as the single verb does since X7-FIX-B, and its batch file is
also hashed pre-lock as a fail-fast.
"""
from __future__ import annotations

import json
import logging

from .. import views
from . import events
from . import db_errors
from . import oversight_policy
from .errors import Refused
from .store_create import _MAX_DEPS

MAX_OPS = 500
_OPS = {
    "create": {"op", "item"},
    "edit": {"op", "key", "fields"},
    "legacy_state": {"op", "key", "state", "note"},
    "add_evidence": {"op", "key", "kind", "ref", "repo"},
    "close_on_decision": {"op", "key"},
    "cancel": {"op", "key", "reason"},
    "priority": {"op", "key", "priority"},
    "rehome": {"op", "key", "new_project", "host"},
    "set_kind": {"op", "key", "kind"},
    "add_dep": {"op", "key", "dep"},
    "remove_dep": {"op", "key", "dep"},
}
_MALFORMED = (TypeError, ValueError, AttributeError, KeyError,
              *db_errors.PROGRAMMING)
#: ops that change a key's state or epoch (evidence after them is verified in-lock).
_MOVES = ("create", "legacy_state", "cancel", "close_on_decision", "rehome")


class OpBatch:
    """What a batch in flight records for the store's hooks."""

    def __init__(self):
        self.current = 0
        self.touched = {}        # op index -> keys its validation concerned
        self.allow_new = set()   # codes an op is expected to introduce
        self.strict = set()      # keys of ops that may introduce NO new code
        self.pre = {}            # (key, kind, ref json) -> (result, state, epoch)

    def note(self, affected, allow_new):
        self.touched.setdefault(self.current, set()).update(affected or ())
        self.allow_new.update(allow_new or ())
        if not allow_new:
            self.strict.update(affected or ())

    def verified(self, item, kind, ref):
        """The pre-lock verification of this evidence, re-checked, or ``None``."""
        hit = self.pre.get((item["key"], kind, _ref_json(ref)))
        if hit is None:
            return None
        result, state, epoch = hit
        if item["state"] != state or int(item["epoch"]) != epoch:
            raise Refused("conflict",
                          f"{item['key']} changed under the transaction")
        return result


def _ref_json(ref):
    return events.canonical(ref or {})


def _op_key(op):
    if not isinstance(op, dict):
        return "-"
    if op.get("op") == "create":
        item = op.get("item")
        item = item[0] if isinstance(item, list) and item else item
        if isinstance(item, dict) and item.get("project") and item.get("id"):
            return f"{item['project']}:{item['id']}"
        return "-"
    return str(op.get("key") or "-")


def _fail(index, op, exc):
    name = op.get("op") if isinstance(op, dict) else "-"
    return Refused(exc.code, f"op {index + 1} {name} {_op_key(op)}: "
                             f"{exc.code} {exc.msg}")


class OpsMixin:
    """The ``apply_ops`` verb (router; a narrow ``oversight`` grant too)."""

    def apply_ops(self, principal, ops, batch_ref=None, base_seq=None,
                  dry_run=False):
        return self._run(principal, "apply_ops", None,
                         lambda now: self._apply_ops_audited(
                             principal, ops, batch_ref, now,
                             base_seq=base_seq, dry_run=dry_run))

    def _apply_ops_audited(self, principal, ops, batch_ref, now, base_seq=None,
                           dry_run=False):
        """Run one batch; an oversight refusal is recorded after the rollback.

        ``_run`` records every oversight refusal at the VERB seam, so this path
        marks the denial it already wrote with ``_oversight_denied_written``:
        the batch row carries the op/finding context, the seam row would not.
        """
        oversight = principal.role == "oversight"
        self._oversight_fail = None
        self._oversight_ops = list(ops) if isinstance(ops, list) else []
        try:
            return self._apply_ops(principal, ops, batch_ref, now,
                                   base_seq=base_seq, dry_run=dry_run)
        except Refused as exc:
            if oversight:
                self._record_oversight_denied(principal, batch_ref, exc, now)
                self._oversight_denied_written = True
            raise

    # ----------------------------------------------------------- planning
    def _plan_ops(self, ops, batch_ref, principal=None):
        if not isinstance(ops, list) or not ops:
            raise Refused("validation", "ops must be a non-empty list")
        if len(ops) > MAX_OPS:
            raise Refused("validation", f"batch exceeds {MAX_OPS} ops")
        for index, op in enumerate(ops):
            try:
                self._check_op_shape(op, batch_ref, principal)
            except Refused as exc:
                raise _fail(index, op, exc) from None

    @staticmethod
    def _check_op_shape(op, batch_ref, principal=None):
        oversight = getattr(principal, "role", None) == "oversight"
        if not isinstance(op, dict):
            raise Refused("validation", "op must be a mapping")
        name = op.get("op")
        if oversight and name == "remove_dep":
            # Shape-check it normally; the policy refuses it with dep_removal.
            pass
        elif oversight and name not in oversight_policy.OVERSIGHT_OPS:
            raise Refused("forbidden",
                          f"oversight:op_denied op {name!r} is not granted")
        if name not in _OPS:
            raise Refused("validation", f"unknown op {name!r}")
        allowed = _OPS[name]
        if oversight:
            allowed = allowed | {"reason", "finding"}
        extra = set(op) - allowed
        if extra:
            raise Refused("validation", f"unexpected fields {sorted(extra)}")
        if name == "create":
            item = op.get("item")
            items = item if isinstance(item, list) else [item]
            if not items or not all(isinstance(i, dict) for i in items):
                raise Refused("validation", "create needs item mapping(s)")
            return
        if not isinstance(op.get("key"), str):
            raise Refused("validation", f"{name} needs a key")
        if name == "priority":
            n = op.get("priority")
            if isinstance(n, bool) or not isinstance(n, int):
                raise Refused("validation", "priority must be an integer")
        if name == "set_kind" and not isinstance(op.get("kind"), str):
            raise Refused("validation", "set_kind needs a kind")
        if name in ("add_dep", "remove_dep") \
                and not isinstance(op.get("dep"), str):
            raise Refused("validation", f"{name} needs a dep")
        if name == "close_on_decision" and not isinstance(batch_ref, str):
            raise Refused("validation", "close_on_decision needs a batch_ref")

    def _prelock(self, ops, batch_ref, batch, now):
        """Slow I/O first; never takes the write lock."""
        moved = set()
        for index, op in enumerate(ops):
            name = op["op"]
            try:
                if name == "close_on_decision":
                    self._read_batch(batch_ref)
                elif name == "add_evidence" and op["key"] not in moved:
                    self._prelock_evidence(op, batch, now)
            except Refused as exc:
                raise _fail(index, op, exc) from None
            if name in _MOVES:
                if name == "create":
                    items = op["item"] if isinstance(op["item"], list) \
                        else [op["item"]]
                    moved.update(f"{i.get('project')}:{i.get('id')}"
                                 for i in items)
                else:
                    moved.add(op["key"])

    def _prelock_evidence(self, op, batch, now):
        item = self._item(op["key"])
        if item is None or item["state"] not in ("claimed", "in_review"):
            return  # the in-lock op reports the real refusal
        ref = self._evidence_ref(op)
        result = self._verify_evidence(item, op["kind"], ref, now)
        batch.pre[(item["key"], op["kind"], _ref_json(ref))] = (
            result, item["state"], int(item["epoch"]))

    @staticmethod
    def _evidence_ref(op):
        ref = dict(op.get("ref") or {})
        if op.get("repo") is not None:
            ref["repo"] = op["repo"]
        return ref

    # ------------------------------------------------------------ applying
    def _deps_from_form(self, item, spec):
        """Compute new depends_on list from {'add': [...], 'remove': [...]} form."""
        if not isinstance(spec, dict) or set(spec) - {"add", "remove"} \
                or not set(spec) & {"add", "remove"}:
            raise Refused("validation",
                          "depends_on form takes only add/remove lists of keys")
        add_list = spec.get("add", [])
        remove_list = spec.get("remove", [])
        if not isinstance(add_list, list) or not isinstance(remove_list, list):
            raise Refused("validation",
                          "depends_on form takes only add/remove lists of keys")
        if len(add_list) > _MAX_DEPS or len(remove_list) > _MAX_DEPS:
            raise Refused("validation",
                          "depends_on form takes only add/remove lists of keys")
        if not all(isinstance(x, str) for x in add_list) or not all(isinstance(x, str) for x in remove_list):
            raise Refused("validation",
                          "depends_on form takes only add/remove lists of keys")
        proj = item["project"]
        current = list(item.get("depends_on") or [])
        rem_set = {self._qualify(proj, d) for d in remove_list}
        new_deps = [d for d in current if d not in rem_set]
        for d in add_list:
            qd = self._qualify(proj, d)
            if qd not in new_deps:
                new_deps.append(qd)
        return new_deps

    def _edit_op(self, principal, key, fields, now):
        """Execute an edit op, expanding depends_on forms and tracking dropped deps."""
        has_deps = isinstance(fields, dict) and "depends_on" in fields
        old_deps = []
        if has_deps:
            item = self._get_or_refuse(key)
            old_deps = list(item.get("depends_on") or [])
            dep_spec = fields["depends_on"]
            if isinstance(dep_spec, dict):
                fields = dict(fields, depends_on=self._deps_from_form(item, dep_spec))
        res = self._edit(principal, key, fields, now)
        if has_deps:
            after_item = self._item(key)
            new_deps_set = set(after_item.get("depends_on") or []) if after_item else set()
            dropped = sorted(set(old_deps) - new_deps_set)
            if dropped:
                res = dict(res, dropped_deps=dropped)
        return res

    def _run_op(self, principal, op, batch_ref, now):
        name = op["op"]
        key = self.resolve_key(op.get("key"))  # an alias made earlier in the batch
        if name == "create":
            item = op["item"]
            return self._create(principal, item if isinstance(item, list)
                                else [item], now, False)
        if name == "edit":
            fields = op["fields"]
            return self._edit_op(principal, key, fields, now)
        if name == "legacy_state":
            return self._set_legacy_state(principal, key, op.get("state"),
                                          op.get("note", ""), now)
        if name == "add_evidence":
            return self._router_evidence(principal, key, op.get("kind"),
                                         self._evidence_ref(op), now)
        if name == "close_on_decision":
            return self._close_on_decision(principal, key, batch_ref, now)
        if name == "priority":
            return self._priority(principal, key, op["priority"], now)
        if name == "set_kind":
            return self._set_kind(principal, key, op["kind"], now)
        if name == "rehome":
            return self._rehome(principal, key, op.get("new_project"),
                                op.get("host"), now)
        if name in ("add_dep", "remove_dep"):
            return self._dep_op(principal, name, key, op["dep"], now)
        return self._cancel(principal, key, op.get("reason", ""), now)

    def _dep_op(self, principal, name, key, dep, now):
        """``add_dep``/``remove_dep``: the item's CURRENT ``depends_on``, edited
        by delegating to ``_edit`` so authorisation, state, the graph
        validation and the ``item_defined`` event are exactly the edit's.

        A dep that is already there (``add_dep``) or not there (``remove_dep``)
        changes nothing: no write and no event, only a warning.  A dep naming a
        cancelled item is dropped by ``_edit`` (with its own warning), never
        refused; an unknown dep key still fails the graph validation.
        """
        item = self._get_or_refuse(key)
        current = list(item.get("depends_on") or [])
        target = self._qualify(item["project"], dep)
        if name == "remove_dep":
            if target not in current:
                return self._dep_noop(principal, item,
                                      [f"dependency {target} not present"])
            deps = [d for d in current if d != target]
        else:
            if target in current:
                return self._dep_noop(principal, item,
                                      [f"dependency {target} already present"])
            deps = current + [target]
        return self._edit(principal, key, {"depends_on": deps}, now)

    def _dep_noop(self, principal, item, warnings):
        """The answer for a dep op that would not change the graph.

        The item still must exist, be unlocked and be editable -- the checks
        ``_edit`` runs before it writes -- but nothing is written and no event
        is appended.
        """
        self._assert_unlocked(item)
        self._may_manage(principal, item["project"])
        if item["state"] != "open":
            raise Refused("bad_state",
                          f"{item['key']} is {item['state']}, not open")
        return {"key": item["key"], "warnings": warnings}

    def _check_base_seq(self, ops, base_seq):
        """Refuse with conflict if any key touched by edit/cancel has events newer than base_seq."""
        check_keys = []
        seen = set()
        for op in ops:
            if op.get("op") in ("edit", "cancel"):
                k = op.get("key")
                if k and k not in seen:
                    seen.add(k)
                    check_keys.append(k)
        if not check_keys:
            return
        stale = []
        for k in check_keys:
            row = self.conn.execute(
                "SELECT 1 FROM events WHERE key=? AND seq>? LIMIT 1",
                (k, base_seq)).fetchone()
            if row is not None:
                stale.append(k)
        if stale:
            stale_str = ", ".join(stale)
            raise Refused("conflict",
                          f"base_seq {base_seq}: changed after base_seq: {stale_str}")

    def _apply_ops(self, principal, ops, batch_ref, now, base_seq=None,
                   dry_run=False):
        oversight = principal.role == "oversight"
        if oversight and (not isinstance(batch_ref, str) or not batch_ref.strip()):
            raise Refused("validation",
                          "oversight:batch_ref_required batch_ref is required")
        self._require_role(principal, "router", "oversight")
        if base_seq is not None:
            if isinstance(base_seq, bool) or not isinstance(base_seq, int) \
                    or base_seq < 0:
                raise Refused("validation",
                              "base_seq must be a non-negative integer")
        if dry_run is not None and not isinstance(dry_run, bool):
            raise Refused("validation", "dry_run must be a boolean")
        dry_run = bool(dry_run)
        self._plan_ops(ops, batch_ref, principal)
        metas = [{} for _ in ops]
        if oversight:
            kept = []
            for index, op in enumerate(ops):
                metas[index] = self._oversight_meta(op, index)
                kept.append({name: value for name, value in op.items()
                             if name not in ("reason", "finding")})
            ops = kept
        self._oversight_meta_list = metas
        ops = [dict(op, key=self.resolve_key(op["key"])) if "key" in op else op
               for op in ops]
        if oversight:
            self._oversight_budget(principal, now)
            self._oversight_gate(principal, ops)
        batch = OpBatch()
        self._prelock(ops, batch_ref, batch, now)
        results = []
        self.conn.execute("BEGIN IMMEDIATE")
        self._batch = batch
        try:
            if base_seq is not None:
                self._check_base_seq(ops, base_seq)
            before = self._all_items()
            if oversight:
                self._oversight_gate(principal, ops, before)
            for index, op in enumerate(ops):
                batch.current = index
                before_seq = self.conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
                results.append(self._guarded_op(principal, index, op,
                                                batch_ref, now))
                # A dry run's rows would roll back with the batch; skipping
                # them makes "no applied audit row, no applied budget" explicit.
                if oversight and not dry_run:
                    self._record_oversight_applied(
                        principal, batch_ref, index, op, metas[index],
                        before_seq, now)
            self._batch = None
            self._final_validation(before, ops, batch)
            if dry_run:
                self.conn.execute("ROLLBACK")
                return {"applied": len(results), "results": results,
                        "dry_run": True}
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        finally:
            self._batch = None
        return {"applied": len(results), "results": results}

    def _oversight_gate(self, principal, ops, items=None):
        """The one policy check, run pre-lock and again in-lock."""
        graph = self._all_items() if items is None else items
        items_by_key = {item["key"]: item for item in graph}
        for verdict in oversight_policy.check_batch(
                items_by_key, ops, principal.projects):
            self._oversight_fail = {
                "index": verdict.index, "op": ops[verdict.index],
                "key": None if verdict.key == "-" else verdict.key,
                "reason": verdict.reason,
                "finding": self._oversight_meta_list[verdict.index].get(
                    "finding", ""),
                "input_reason": self._oversight_meta_list[verdict.index].get(
                    "reason", "")}
            raise _fail(verdict.index, ops[verdict.index],
                        Refused(verdict.code, verdict.message))

    def _guarded_op(self, principal, index, op, batch_ref, now):
        try:
            return self._run_op(principal, op, batch_ref, now)
        except Refused as exc:
            if principal.role == "oversight":
                self._oversight_fail = {
                    "index": index, "op": op,
                    "key": None,
                    "reason": self._reason_from_msg(exc.msg),
                    "finding": self._oversight_meta_list[index].get(
                        "finding", ""),
                    "input_reason": self._oversight_meta_list[index].get(
                        "reason", "")}
            raise _fail(index, op, exc) from None
        except db_errors.INTEGRITY as exc:
            raise _fail(index, op, Refused(
                "conflict", "keeper data conflicts with this request")) from exc
        except _MALFORMED as exc:
            logging.getLogger("plangraph.keeper").exception(
                "malformed input for apply_ops op %s %s", index + 1, op.get("op"))
            raise _fail(index, op, Refused(
                "validation", "malformed input")) from exc

    def _final_validation(self, before, ops, batch):
        """Whole-graph validation ONCE over the final graph."""
        touched = set().union(*batch.touched.values()) if batch.touched else set()
        try:
            self._validate_change(self._all_items(), before, affected=touched,
                                  allow_new=batch.allow_new)
            self._refuse_strict_allow_new(before, batch)
        except Refused as exc:
            owner = next((i for i, keys in sorted(batch.touched.items())
                          if any(k in exc.msg for k in keys)), None)
            if owner is None:
                raise Refused(exc.code, f"op - validate: {exc.code} "
                                        f"{exc.msg}") from None
            raise _fail(owner, ops[owner], exc) from None

    def _refuse_strict_allow_new(self, before, batch):
        """``allow_new`` (a cancel's ``depends_on_cancelled``) is batch-wide in
        ``_validate_change``; keep it from excusing an item that a create or edit
        op of the batch introduced, exactly as the single verbs would refuse it."""
        if not (batch.allow_new and batch.strict):
            return
        known = {(p["code"], p["key"]) for p in views.validate(before)}
        bad = [p for p in views.validate(self._all_items())
               if p["code"] in batch.allow_new and p["key"] in batch.strict
               and (p["code"], p["key"]) not in known]
        if bad:
            raise Refused("validation", "; ".join(
                f"{p['code']}:{p['key']}:{p['msg']}" for p in bad))
