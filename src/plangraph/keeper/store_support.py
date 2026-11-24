"""Shared plumbing for :class:`plangraph.keeper.store.Store`.

None of these are verbs: they are the row hydration helpers, the timestamp
codec, the event append and the evidence-verification helpers that every verb
group reuses.  They live on a mixin so ``Store`` keeps one public API
(``SupportMixin`` is first in its MRO) while ``store.py`` stays a readable
transaction/authorisation core.
"""
from __future__ import annotations

import contextlib
import json
import re
from collections import Counter
from datetime import datetime, timezone

from .. import views
from . import events
from .errors import Refused
from .evidence_floor import event_bases, ref_head
from .evidence_types import Context, repo_identity, valid_commit_ref
from .item_fixes import ErrorRecordVerifier, check_error_ref

__all__ = ["SupportMixin", "iso", "parse_iso"]

#: Refusal timestamps kept per key; a key refused more often inside the window counts this many.
REFUSAL_TIMES_CAP = 256

_TERMINAL_EVENTS = frozenset({"cancelled", "closed_on_decision", "answered",
                              "review_passed"})


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


#: A decision row that refuses or cancels the work vouches for no merge, even when it names the sha.
_REFUSAL_ROW = re.compile(r"\bno[- ]go\b|\bcancel(?:l?ed)?\b|\brevert(?:ed)?\b", re.I)


class SupportMixin:
    """Row hydration, event append and evidence-verification helpers."""

    # ------------------------------------------------ merge-close helpers
    def _one_name_per_repository(self, names):
        """*names* with each repository (resolved path and ref) kept once, under its first name.

        keeper.json can register one directory under several names; the commits of
        such a directory are one candidate, not an ambiguity.
        """
        git, seen, kept = self._adapter("git"), set(), []
        for name in names:
            path = git.repo_path(name) if callable(getattr(git, "repo_path", None)) else None
            ref = git.repo_ref(name) if callable(getattr(git, "repo_ref", None)) else "main"
            ident = repo_identity(path, ref) if path else (name, None)
            if ident not in seen:
                seen.add(ident)
                kept.append(name)
        return kept

    def _lane_merge_decision(self, item, merge, now):
        """The D-id of a pre-PR lane merge: named in its subject, and its row names this merge sha and the item.

        ``None`` when the subject names no decision (the merge has no proof at all);
        a refusal in the ``lane_merge_unproven`` class when a named row does not vouch for it.
        """
        sha, subject = merge.get("sha"), str(merge.get("subject") or "")
        ids = list(dict.fromkeys(re.findall(r"\((D-[0-9]+)(?![\w-])", subject)))
        if not ids or not valid_commit_ref(sha):
            return None
        from .store_waiver import _names_item as names_item  # circular at import time
        # A frozen close reads the rows the warm scan captured (none captured: nothing vouches);
        # any other close reads the document once.
        frozen = self.__dict__.get("_frozen_lanes")
        texts = ((self.__dict__.get("_landing_decisions") or {}).get("texts") or {}) if frozen else {}
        with contextlib.nullcontext() if frozen else self._decisions_snapshot(item, now):
            for did in ids:
                if frozen:
                    text, known = texts.get(did), did in texts
                else:
                    decisions = self._ctx(item, now).decisions
                    try:
                        text = decisions.text(did) if decisions.has(did) else None
                    except Exception:  # noqa: BLE001 - an unreadable row never vouches
                        text = None
                    known = text is not None and self._verify_evidence(item, "decision", {"id": did}, now).ok
                if (text and known and not _REFUSAL_ROW.search(text) and re.search(rf"(?<![0-9a-fA-F]){re.escape(sha)}(?![0-9a-fA-F])", text, re.I)
                        and names_item(text, item["key"])):
                    return did
        raise Refused("evidence_unverified", f"lane_merge_unproven: {sha[:12]} {', '.join(ids)}")

    def _lane_merge_proof(self, item, merge, parents, go_pr, go_head, now):
        """The ``lane_merge`` proof of a merge without a PR number, ``None`` when none applies.

        A merge whose subject names no decision, or one a GO row bound to its PR already
        explains, returns ``None``; a named row that does not vouch for the merge refuses
        as ``lane_merge_unproven`` unless that GO binding can still prove it.
        """
        try:
            did = self._lane_merge_decision(item, merge, now)
        except Refused:
            if go_pr and valid_commit_ref(go_head):
                return None
            raise
        return None if did is None else {"pr": None, "head": parents[1], "decision_id": did,
                                         "merge_sha": merge["sha"], "kind": "lane_merge"}

    # ------------------------------------------------------- verification
    def _ctx(self, item, now):
        """The verifier context for *item* at *now*.

        The injected ``ctx`` supplies the adapters; the store always overrides
        ``now`` and derives ``claim_time`` from the item's latest ``claimed``
        event, so the real verifiers compare a commit against the actual claim
        instant rather than a caller-supplied one.  ``bind_floor_sha``,
        ``bind_floor_shas`` and ``bind_floor_required`` supply the ancestry
        floor for trailer-bound commits (see :meth:`_bind_floor`).
        ``merge_principals`` passes through the trusted merge actor principals
        from ``ctx``.
        """
        floor_sha, floor_required, floor_bases = self._bind_floor(item)
        raw_principals = self._adapter("merge_principals")
        if raw_principals is None:
            merge_principals: tuple[str, ...] = ()
        elif isinstance(raw_principals, (list, tuple, set, frozenset)):
            merge_principals = tuple(str(p) for p in raw_principals)
        elif isinstance(raw_principals, str):
            merge_principals = (raw_principals,)
        else:
            try:
                merge_principals = tuple(str(p) for p in raw_principals)
            except TypeError:
                merge_principals = (str(raw_principals),)

        return Context(
            git=self._adapter("git"),
            ci=self._adapter("ci"),
            runs=self._adapter("runs"),
            semaphore=self._adapter("semaphore"),
            decisions=self._adapter("decisions"),
            spawner_records=self._adapter("spawner_records"),
            claude_projects_dir=self._adapter("claude_projects_dir"),
            now=now,
            claim_time=self._claim_time(item),
            bind_floor_sha=floor_sha,
            bind_floor_shas=floor_bases,
            bind_floor_required=floor_required,
            merge_principals=merge_principals,
        )

    def _adapter(self, name: str):
        """Look up an adapter by *name* from the injected ``ctx``."""
        base = self.ctx
        if base is None:
            return None
        if isinstance(base, dict):
            return base.get(name)
        return getattr(base, name, None)

    def _ref_head(self, project: str) -> str | None:
        """Best-effort head SHA of *project*'s target ref, or ``None``."""
        return ref_head(self._adapter("git"), project)

    def _claim_time(self, item):
        """The epoch of the item's current claim, or ``None`` if never claimed."""
        row = self.conn.execute(
            "SELECT ts FROM events WHERE key=? AND type='claimed'"
            " ORDER BY seq DESC LIMIT 1", (item["key"],)).fetchone()
        if row is None:
            return None
        parsed = parse_iso(row["ts"])
        return parsed.timestamp() if parsed is not None else None

    def _bind_floor(self, item: dict) -> tuple[str | None, bool, object]:
        """The floor commit SHA, requirement flag and per-repo bases for *item*.

        Returns ``(floor_sha, required, bases)``.  A first claim has no floor
        event and returns ``(None, False, {})``.  When a floor event exists (a
        prior release or claim by another principal), its recorded ``base``
        commit is returned as ``(base, True, bases)``, or ``(None, True,
        bases)`` if unrecorded or invalid (failing closed).  ``bases`` is the
        event's per-repo head map: ``{}`` for a legacy event, the raw value
        when it is malformed so resolution fails closed (D-3335).
        """
        claimant = item.get("claimed_by") or ""
        row = self.conn.execute(
            "SELECT payload FROM events WHERE key=? AND ("
            "type='released' OR (type='claimed' AND COALESCE(actor, '') != ?))"
            " ORDER BY seq DESC LIMIT 1",
            (item["key"], claimant),
        ).fetchone()
        if row is None:
            return (None, False, {})
        payload = _json_dict(row["payload"])
        bases = event_bases(payload)
        base = payload.get("base") if isinstance(payload, dict) else None
        if isinstance(base, str) and valid_commit_ref(base) and base == base.lower():
            return (base, True, bases)
        return (None, True, bases)

    def verify_chain(self):
        return events.verify_chain(self.conn)

    def verify_integrity(self):
        return events.verify_integrity(self.conn)

    def _append(self, principal, key, type, payload, now, *,
                audience_key=None, recipient=None, prior_holders=()):
        from .store_changelog import record_audience
        out = events.append_event(
            self.conn, iso(now), getattr(principal, "name", None),
            getattr(principal, "audit_role", None) or getattr(principal, "role", None),
            None, key, type, payload)
        record_audience(self.conn, out[0], audience_key or key, type, payload,
                        recipient, prior_holders)
        self._revoke_for_event(principal, key, type, now, payload)
        if key is not None and (type in _TERMINAL_EVENTS or (
                type in ("legacy_state_set", "state_set")
                and (payload or {}).get("state") in ("done", "cancelled"))):
            self._alerts_resolve(key, now)
        elif key is not None and type in ("changes_requested", "submitted"):
            self._alerts_resolve(key, now, code="review_overdue")
        return out

    # ------------------------------------------------------- alerts table
    # KEEPER-ALERTS-TABLE-ON-RAISE-CLEAR: ``alerts`` holds what is wrong now.  A row
    # is written when an alert is raised and resolved when it is cleared, each with
    # one ``alert_log`` telemetry row; ``alerts()`` reads only this table.
    def _alert_telemetry(self, action, key, code, now):
        self.conn.execute(
            "INSERT INTO alert_log(ts,action,key,code) VALUES(?,?,?,?)",
            (iso(now), action, key, code))

    def _alert_row(self, kind, key, code):
        where = "key IS NULL" if key is None else "key=?"
        args = (kind,) if key is None else (kind, key)
        sql = f"SELECT * FROM alerts WHERE kind=? AND {where} AND code=?"
        return self.conn.execute(sql, (*args, code)).fetchone()

    def _alerts_raise_finding(self, key, code, msg, now):
        ts = iso(now)
        row = self._alert_row("finding", key, code)
        if row is None or row["state"] != "open":
            if row is None:
                self.conn.execute(
                    "INSERT INTO alerts(kind,key,code,msg,first_seen,last_seen,n,state)"
                    " VALUES('finding',?,?,?,?,?,1,'open')", (key, code, msg, ts, ts))
            else:
                self.conn.execute(
                    "UPDATE alerts SET msg=?, first_seen=?, last_seen=?, n=1,"
                    " state='open' WHERE id=?", (msg, ts, ts, row["id"]))
            self._alert_telemetry("alert_raised", key, code, now)
        else:
            self.conn.execute(
                "UPDATE alerts SET msg=?, last_seen=?, n=n+1 WHERE id=?",
                (msg, ts, row["id"]))

    def _watch_merged_open(self, report):
        """Store one successful scan observation and alert after two nonzero passes."""
        if any("merge_index_" in row["reason"] for row in report["listed"]):
            raise Refused("evidence_unverified", "merged-open watcher scan incomplete")
        count = report["merged_not_done"]
        code, now = "merged_open_reconcile", self.now()
        ts = iso(now)
        message = f"{count} merged items need routing reconciliation"
        with self._write():
            prior = self._alert_row("finding", None, code)
            if count and prior and prior["n"]:
                self._alerts_raise_finding(None, code, message, now)
                return True
            if prior is None:
                self.conn.execute(
                    "INSERT INTO alerts(kind,code,msg,first_seen,last_seen,n,state)"
                    " VALUES('finding',?,?,?,?,?,'resolved')", (code, message, ts, ts, int(bool(count))))
            else:
                self.conn.execute(
                    "UPDATE alerts SET msg=?,last_seen=?,n=?,state='resolved' WHERE id=?",
                    (message, ts, int(bool(count)), prior["id"]))
                if prior["state"] == "open":
                    self._alert_telemetry("alert_cleared", None, code, now)
            return False

    def _refusal_times(self, row):
        """Epoch seconds of the refusals a refusal row remembers, oldest first.

        ``msg`` holds the JSON list; a row without it (written before the list existed)
        counts its ``n`` refusals at ``last_seen``.
        """
        try:
            times = json.loads(row["msg"]) if row["msg"] else None
        except ValueError:
            times = None
        if isinstance(times, list) and all(
                isinstance(t, (int, float)) and not isinstance(t, bool) for t in times):
            return [float(t) for t in times]
        last = parse_iso(row["last_seen"])
        return [last.timestamp()] * min(row["n"], REFUSAL_TIMES_CAP) if last else []

    def _alerts_count_refusal(self, key, now):
        """One refused call on *key*: an alert from the second refusal inside the window.

        The row remembers each refusal's time; a refusal older than ``alert_window_s`` stops
        counting on its own, however recent the refusals after it are.
        """
        ts = iso(now)
        row = self._alert_row("refusal", key, "refused")
        if row is None:
            self.conn.execute(
                "INSERT INTO alerts(kind,key,code,msg,first_seen,last_seen,n,state)"
                " VALUES('refusal',?,'refused',?,?,?,1,'open')", (key, json.dumps([now]), ts, ts))
            return
        cutoff = now - self.alert_window_s
        current = ([t for t in self._refusal_times(row) if t >= cutoff]
                   if row["state"] == "open" else [])
        times = (current + [now])[-REFUSAL_TIMES_CAP:]
        self.conn.execute(
            "UPDATE alerts SET msg=?, first_seen=?, last_seen=?, n=?, state='open' WHERE id=?",
            (json.dumps(times), iso(times[0]), ts, len(times), row["id"]))
        if len(current) == 1:
            self._alert_telemetry("alert_raised", key, "refused", now)

    def _alerts_resolve(self, key, now, older_than=None, *, code=None):
        """Clear every open alert of *key* (or, with *key* None, those not seen since *older_than*)."""
        if key is None:
            rows = self.conn.execute(
                "SELECT id,key,code,kind,n FROM alerts WHERE state='open'"
                " AND last_seen < ?", (older_than,)).fetchall()
        else:
            sql = "SELECT id,key,code,kind,n FROM alerts WHERE state='open' AND key=?"
            rows = self.conn.execute(sql if code is None else sql + " AND code=?",
                                     (key,) if code is None else (key, code)).fetchall()
        for row in rows:
            self.conn.execute("UPDATE alerts SET state='resolved' WHERE id=?",
                              (row["id"],))
            if row["kind"] == "finding" or row["n"] >= 2:
                self._alert_telemetry("alert_cleared", row["key"], row["code"], now)
        return len(rows)

    def _alerts_expire(self, now):
        """Resolve alerts not seen inside ``alert_window_s`` (called by the reaper)."""
        return self._alerts_resolve(None, now, older_than=iso(now - self.alert_window_s))

    def backfill_alerts(self, now=None):
        """One-time, idempotent: build ``alerts`` from ``audit_findings`` and refused ``audit_log`` rows.

        The ``alerts_backfill`` ``alert_log`` row is the marker, checked and written in the same
        transaction as the rows. An alert identity (finding: key and code; refusal: key) the
        table already holds, raised live since the table appeared, is merged into its one row:
        the earlier first sight, the later last sight, the larger count.
        """
        now = self.now() if now is None else now
        cutoff = now - self.alert_window_s
        open_states = ("open", "claimed", "in_review")
        with self._write():
            if self.conn.execute("SELECT 1 FROM alert_log WHERE action='alerts_backfill'"
                                 " LIMIT 1").fetchone() is not None:
                return {"findings": 0, "refusals": 0, "skipped": True}
            state = {r["key"]: r["state"] for r in
                     self.conn.execute("SELECT key,state FROM items")}
            merged = {}
            for row in self.conn.execute(
                    "SELECT ts,key,code,msg FROM audit_findings ORDER BY id"):
                if row["key"] is not None and row["key"] not in state:
                    continue
                entry = merged.setdefault((row["key"], row["code"]),
                                          {"first": row["ts"], "last": row["ts"],
                                           "msg": row["msg"], "n": 0})
                # Bounds by time, not by row order: a clock set back must not invert them.
                entry["first"] = min(entry["first"], row["ts"])
                if row["ts"] >= entry["last"]:
                    entry.update(last=row["ts"], msg=row["msg"])
                entry["n"] += 1
            refused = {}
            for row in self.conn.execute(
                    "SELECT key, ts FROM audit_log WHERE ok=0 AND key IS NOT NULL ORDER BY id"):
                refused.setdefault(row["key"], []).append(row["ts"])
            for (key, code), e in merged.items():
                when = parse_iso(e["last"])
                current = (state.get(key, "open") in open_states
                           and (when is None or when.timestamp() >= cutoff))
                have = self._alert_row("finding", key, code)
                if have is None:
                    self.conn.execute(
                        "INSERT INTO alerts(kind,key,code,msg,first_seen,last_seen,n,state)"
                        " VALUES('finding',?,?,?,?,?,?,?)",
                        (key, code, e["msg"], e["first"], e["last"], e["n"],
                         "open" if current else "resolved"))
                    continue
                later = e["last"] >= (have["last_seen"] or "")
                self.conn.execute(
                    "UPDATE alerts SET msg=?, first_seen=?, last_seen=?, n=?, state=? WHERE id=?",
                    (e["msg"] if later else have["msg"],
                     min(have["first_seen"] or e["first"], e["first"]),
                     max(have["last_seen"] or "", e["last"]), max(have["n"], e["n"]),
                     "open" if current or have["state"] == "open" else "resolved", have["id"]))
            for key, stamps in refused.items():
                if key not in state:
                    continue
                stamps = sorted(stamps)
                audited = [t.timestamp() for t in (parse_iso(x) for x in stamps)
                           if t is not None and t.timestamp() >= cutoff]
                first, last, total = stamps[0], stamps[-1], len(stamps)
                have = self._alert_row("refusal", key, "refused")
                live_open = have is not None and have["state"] == "open"
                # A live row keeps its precise times; the audit log (whole seconds) only adds
                # the refusals the row does not already hold, counted per second.
                live = ([t for t in self._refusal_times(have) if t >= cutoff]
                        if live_open else [])
                # Per whole second: the audit rows beyond the live times of that second.
                held = Counter(int(t) for t in live)
                extra = []
                for second, count in Counter(int(t) for t in audited).items():
                    extra += [float(second)] * max(0, count - held[second])
                times = sorted(live + extra)[-REFUSAL_TIMES_CAP:]
                current = state[key] in open_states and len(times) >= 2
                if have is None:
                    self.conn.execute(
                        "INSERT INTO alerts(kind,key,code,msg,first_seen,last_seen,n,state)"
                        " VALUES('refusal',?,'refused',?,?,?,?,?)",
                        (key, json.dumps(times) if current else None, first, last,
                         len(times) if current else total, "open" if current else "resolved"))
                    continue
                bounds = (min(have["first_seen"] or first, first),
                          max(have["last_seen"] or "", last))
                if live_open:
                    # The count stays what the row had raised, so the history of an alert that
                    # lost a refusal to the window stays in `history.refusal_keys`.
                    self.conn.execute(
                        "UPDATE alerts SET msg=?, first_seen=?, last_seen=?, n=? WHERE id=?",
                        (json.dumps(times), *bounds, max(have["n"], len(times)), have["id"]))
                elif current:
                    self.conn.execute(
                        "UPDATE alerts SET msg=?, first_seen=?, last_seen=?, n=?, state='open'"
                        " WHERE id=?", (json.dumps(times), *bounds, len(times), have["id"]))
                else:
                    self.conn.execute(
                        "UPDATE alerts SET first_seen=?, last_seen=?, n=? WHERE id=?",
                        (*bounds, max(have["n"], total), have["id"]))
            self.conn.execute(
                "INSERT INTO alert_log(ts,action,msg) VALUES(?,?,?)",
                (iso(now), "alerts_backfill",
                 f"findings={len(merged)} refusals={len(refused)}"))
        return {"findings": len(merged), "refusals": len(refused), "skipped": False}

    # -------------------------------------------------------------- reads
    def _item(self, key):
        row = self.conn.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        item = self._hydrate(dict(row))
        item["depends_on"] = sorted(row["dst"] for row in self.conn.execute(
            "SELECT dst FROM edges WHERE src=?", (key,)))
        return item

    def _item_neighborhood(self, item):
        """The state and links needed for a single item's context."""
        key = item["key"]
        index = {key: item}
        for row in self.conn.execute(
                "WITH adjacent AS (SELECT dst AS key FROM edges WHERE src=?"
                " UNION SELECT src FROM edges WHERE dst=?)"
                " SELECT key,state,corrects,fixes,EXISTS(SELECT 1 FROM edges"
                " WHERE src=items.key AND dst=?) AS dependent FROM items"
                " WHERE key IN (SELECT key FROM adjacent) OR corrects=? OR fixes=?"
                " OR corrects IN (SELECT key FROM adjacent)", (key,) * 5):
            if row["key"] != key:
                index[row["key"]] = {**dict(row), "depends_on": [key] if row["dependent"] else []}
        return index

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

    @staticmethod
    def _evidence_record(row):
        record = dict(row)
        record["ref"] = _json_dict(record.get("ref"))
        record["verified"] = bool(record["verified"])
        return record

    def _evidence(self, key):
        rows = self.conn.execute(
            "SELECT id,key,epoch,kind,ref,verified,detail,at FROM evidence"
            " WHERE key=? ORDER BY id", (key,)).fetchall()
        return [self._evidence_record(row) for row in rows]

    def _evidence_by_key(self):
        """``key -> [record]`` for every item in one query (same rows as ``_evidence``)."""
        out = {}
        for row in self.conn.execute(
                "SELECT id,key,epoch,kind,ref,verified,detail,at FROM evidence"
                " ORDER BY id"):
            out.setdefault(row["key"], []).append(self._evidence_record(row))
        return out

    def _verified_evidence_ids(self, item):
        """Row ids of the current epoch's *verified* evidence."""
        return frozenset(
            e["id"] for e in self._evidence(item["key"])
            if e["epoch"] == item["epoch"] and e["verified"])

    # -------------------------------------------------- evidence rules
    def _verify_evidence(self, item, kind, ref, now):
        """Run the verifier for *kind*; the slow I/O a verb does pre-txn."""
        # Reject malformed decision refs even with an injected/cached verifier:
        # failed verification may be stored, but an unusable reference must not be.
        if kind == "decision" and (
                not isinstance(ref, dict)
                or not isinstance(ref.get("id"), str)
                or not ref["id"].strip()):
            raise Refused("validation", "decision ref requires a non-empty string id")
        if kind == "claude-seat":
            item = dict(item, _seat_writer_families=sorted(self._merged_writer_families(item, now)))
        if self._batch is not None and kind != "claude-seat":
            done = self._batch.verified(item, kind, ref)
            if done is not None:
                return done
        if kind == "error":  # always unverified, no I/O: never injected
            ref = check_error_ref(ref, item["key"])
            return ErrorRecordVerifier().verify(item, ref, None)
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

    def _reviewer_family_ok(self, principal, item, ctx=None, *,
                            change_request=False):
        """Refuse reviewers matching any authoritative writer family.

        A legacy import claims by an ``owner`` string that need not be a
        principal; its ``owner_family`` (set by the router at import time) then
        stands in for the claimant's family.  Either peer with an unknown family
        fails closed, as does a reviewer with none.

        Current verified commit writers take precedence over writer_model and
        the legacy claimant/delegate fallback. Resolver failures never fall back.

        ``change_request`` is the ONLY exemption, and it is the same on every
        tier: a reviewer who shares a family with a writer may push an item
        back to ``claimed`` (a ``verdict(ok=False)``), but may never record an
        ACCEPT seat -- first or later, standard or high, commit writers or the
        writer_model/claimant/delegate fallback.
        """
        from . import auth
        reviewer_family = auth.norm_family(principal.family)
        if reviewer_family is None:
            raise Refused("family_unknown", "reviewer has no family")
        peers = []
        has_commits = any(e["epoch"] == item["epoch"] and e["verified"]
                          and e["kind"] == "commit+ci"
                          for e in self._evidence(item["key"]))
        if has_commits:
            from .coverage import writer_families
            if ctx is None:
                ctx = self.ctx
            peers.extend(writer_families(self, item, ctx))
        elif item.get("writer_model"):
            from ..review.family import model_family
            family = model_family(item["writer_model"])
            if family is None:
                raise Refused("family_unknown", "writer model family unknown")
            peers.append(family)
        else:
            if item["claimed_by"] == "system":
                raise Refused("writer_unknown", "system claim has no verified commit writers")
            claimer = auth.get_principal(self.conn, item["claimed_by"])
            claimant_family = (claimer.family if claimer is not None
                               else item.get("owner_family"))
            claimant_family = auth.norm_family(claimant_family)
            if claimant_family is None:
                raise Refused("family_unknown", "claimant family unknown")
            peers.append(claimant_family)
        if not has_commits and not item.get("writer_model") and item["delegate"]:
            delegate = auth.get_principal(self.conn, item["delegate"])
            delegate_family = (auth.norm_family(delegate.family)
                               if delegate is not None else None)
            if delegate_family is None:
                raise Refused("family_unknown", "delegate family unknown")
            peers.append(delegate_family)
        # Every ACCEPT seat carries the strict check; only a change request
        # (``ok=False``) may come from a writer's own family, so a reviewer
        # whose family is a peer can never push an item towards ``done``.
        if reviewer_family in peers and not change_request:
            raise Refused("same_family", f"reviewer shares item family {reviewer_family}")

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
        Inside ``apply_ops`` the call is only recorded: the batch validates once.
        """
        if self._batch is not None:
            self._batch.note(affected, allow_new)
            return
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
