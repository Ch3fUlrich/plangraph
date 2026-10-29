"""Review verbs for :class:`plangraph.keeper.store.Store`.

D-228 risk-tiered reviews: how many passing verdicts ``done`` needs depends on
the item's ``review_tier``, which the router (or L1, through the router's
defaults) fixes at definition time.

* ``standard`` (the default): **one** passing ``verdict(ok=True)`` from a
  reviewer who is neither the claimant nor the delegate and whose normalised
  non-blank family differs from the writer's (``_reviewer_family_ok`` fails
  closed on a blank family, for either side) records ``review_verdict`` and
  writes ``review_passed`` (``done``, locked) immediately.
* ``high`` — D-3084 supersedes D-219's fixed second-verdict close: a passing
  ``verdict(ok=True)`` records a ``review_verdict`` event and leaves the item
  in review; from the second verdict on the round accumulates ACCEPT seats and
  closes (``review_passed``, ``done``) on the verdict that completes the
  per-part seat coverage (``coverage.uncovered`` with ``need=2`` and
  ``sensitive=True``).  A verdict that leaves coverage incomplete lands as
  another seat and its result carries the missing writer families; a
  third-or-later verdict is accepted only while the seats so far still miss a
  writer family and is refused (``conflict``) once they already cover.  Seat
  families never count twice: unless the waiver is active the reviewer must
  differ from every prior seat's family (``same_family``).

A ``verdict(ok=False)`` from either seat returns the item to ``claimed`` exactly
as before and, because the next ``submit`` advances the submission ``round``,
discards the round's earlier verdicts.  A second change request only reopens
when it is consecutive *within the current epoch*: change requests from earlier
claims are ignored and a passing verdict resets the count.

The writer-family gate (``_reviewer_family_ok``) covers BOTH seats of BOTH
tiers and, per D-3084, follows the per-part rule: seats cover parts, not
items, so an ACCEPT seat (``ok=True``) is refused ``same_family`` only when
the reviewer's family is the item's SOLE writer family — the seat would cover
no part — while a family that writes one part but not another may take an
ACCEPT seat whose worth the per-part coverage gate decides.  Either way a
reviewer from a writer's own family may file a change request (``ok=False``),
and an unknown family on either side still fails closed.

The waiver path (D-217) lives in ``store_waiver``: once the router records a
verified decision, ``_waiver_active`` is true and the second passing verdict on
a ``high`` item may share the first reviewer's family.  It is a no-op on a
``standard`` item (``bad_state``), which never had a second seat to fill.
"""
from __future__ import annotations

import json

from . import auth
from .errors import Refused


class ReviewMixin:
    """The ``verdict`` verb and the two-reviewer bookkeeping."""

    # ----------------------------------------------------------- verdict
    def verdict(self, principal, key, ok, reason=""):
        result = self._run(principal, "verdict", key,
                           lambda now: self._verdict(principal, key, ok, reason,
                                                     now))
        # RT-P1-JEV-SHADOW: post-commit, outside ``_write`` - see the helper.
        self._shadow_qc_accept(principal, key, ok, reason)
        return result

    def _shadow_qc_accept(self, principal, key, ok, reason):
        """Record ONE observe-only ``qc_accept`` row for the verdict just landed.

        The keeper's own accept/reject decision, shadowed exactly like
        ``review.actions.plan_actions`` shadows the planner's: through its
        injected observation hook, so the same switch
        (``PLANGRAPH_JEV``, off is a cheap no-op with no ask and no network),
        the same redaction, the same single guarded ask
        (``jev_client.MAX_TIMEOUT_S``) and the same ``[]``-on-failure rules
        apply.  ``ok`` is the rule's answer - ``accept`` for a pass, ``reject``
        for a change request - and only the verdict's evidence (reason) is
        handed to the state builder, never the word itself.

        It dispatches AFTER the verdict commits, without waiting for the ask.
        Its observer owns a separate connection and fills only its own row's
        accepted verdict outcome. The rules result is returned unchanged.

        The row is filed by :func:`plangraph.keeper.store_jev._jev_writer`, the
        shared writer mapping the worker verbs' outcome fill also uses: a
        verdict can only come from a ``reviewer``, who may not write
        ``jev_shadow``, so the keeper's own observation of the verdict rides on
        a principal that may.
        """
        try:
            # Local imports: the planner is only loaded when a hook can run, and
            # ``store_jev`` (which owns the writer mapping) is loaded with it.
            from .store_jev import _jev_writer
            from plangraph.planner import jev_shadow, jev_client
            cfg = jev_client.config_from_env()
            if not jev_client.enabled(cfg):
                return
            verdict = {"verdict": "pass" if ok else "fix", "reason": reason}
            writer = _jev_writer(principal)
            connect = jev_shadow.observer_connection(self)

            def sink(row):
                from .store import Store
                conn = connect()
                try:
                    observer = Store(conn, clock=self.clock)
                    result = observer.jev_record(writer, row)
                    # Fill exactly this decision, not another reviewer's rows.
                    with observer._write():
                        conn.execute(
                            "UPDATE jev_shadow SET outcome=?, outcome_at=? WHERE id=? AND outcome IS NULL",
                            ("accept" if ok else "reject", self._iso(self.clock()), result["id"]))
                finally:
                    conn.close()
                return True

            from plangraph.review.actions import plan_actions
            plan_actions(verdict, "review", key, store=self, principal=writer,
                         shadow=lambda observed, phase, card: jev_shadow.background(
                             lambda: jev_shadow.shadow_qc_accept(
                                 observed, phase, card, sink=sink, cfg=cfg)))
        except Exception:  # noqa: BLE001 - a shadow failure never breaks a verdict
            pass

    def _waiver_active(self, item):
        """D-217 hook: a verified waiver lets the two verdicts share a family."""
        # The same truthiness question ``digest`` asks, so an item can never be
        # "waived" to one of them and not the other.
        return bool(item.get("review_waiver"))

    def _epoch_change_streak(self, item):
        """Trailing ``changes_requested`` events with no pass since, this epoch.

        The lifetime count this replaces was a liveness bug: one change request
        in an earlier claim made the first change request of every later claim
        reopen the item and file a false ``changes_requested_twice`` finding.
        'Twice in a row' is consecutive *within the current epoch*, and a
        passing verdict resets the streak.
        """
        epoch = int(item["epoch"])
        streak = 0
        for row in self.conn.execute(
                "SELECT type, payload FROM events WHERE key=?"
                " AND type IN ('changes_requested', 'review_verdict')"
                " ORDER BY seq", (item["key"],)):
            payload = json.loads(row["payload"] or "{}")
            # An event without an epoch predates epoch tracking (or was
            # hand-written): it can never be "consecutive within this epoch",
            # so it is ignored rather than counted.
            if int(payload.get("epoch", -1)) != epoch:
                continue
            if row["type"] == "review_verdict":
                if payload.get("ok", True):
                    streak = 0
            else:
                streak += 1
        return streak

    def _round_verdicts(self, item):
        """Passing ``review_verdict`` payloads for this item's current round.

        Verdicts are tied to the submission round they judged: a changed verdict
        returns the item to ``claimed`` and the next ``submit`` bumps ``round``,
        so the earlier round's verdicts no longer match.
        """
        return self._current_round(item, [
            json.loads(row["payload"] or "{}") for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='review_verdict'"
                " ORDER BY seq", (item["key"],))])

    def _verdicts_by_key(self):
        """``key -> [payload]`` of every ``review_verdict`` in one query (seq order)."""
        out = {}
        for row in self.conn.execute(
                "SELECT key,payload FROM events WHERE type='review_verdict' ORDER BY seq"):
            out.setdefault(row["key"], []).append(json.loads(row["payload"] or "{}"))
        return out

    @staticmethod
    def _current_round(item, payloads):
        epoch, round_no = int(item["epoch"]), int(item.get("round") or 0)
        return [p for p in payloads
                if int(p.get("epoch", -1)) == epoch and int(p.get("round", -1)) == round_no]

    def _verdict(self, principal, key, ok, reason, now):
        self._require_role(principal, "reviewer")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "in_review":
            raise Refused("bad_state", f"{key} is {item['state']}, not in review")
        if (item.get("review_tier") or "standard") == "none":
            raise Refused("bad_state",
                          f"{key} is review_tier=none:"
                          " closes only via close_on_decision")
        if principal.name in (item["claimed_by"], item["delegate"]):
            raise Refused("same_principal", "reviewer is the claimant")
        self._require_project(principal, item["project"])
        # ``change_request=not ok``: the family gate is strict for every ACCEPT
        # seat and only lets a change request (ok=False) through.
        self._reviewer_family_ok(principal, item, self._ctx(item, now),
                                 change_request=not ok)
        if not ok:
            return self._verdict_changes(principal, key, reason, item, now)
        self._require_evidence(item)
        # The slow re-verification runs before the write lock; it records exactly
        # which evidence rows it considered so the transaction can prove that set
        # is unchanged.  It runs for the first verdict too, so a stale-evidence
        # refusal never depends on a peer showing up first.
        considered = self._reverify_all(item, now)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != "in_review":
                raise Refused("conflict",
                              f"{key} is {fresh['state']}, not in review")
            if int(fresh["epoch"]) != int(item["epoch"]):
                raise Refused("conflict", f"{key} changed under the transaction")
            if (fresh.get("review_tier") or "standard") == "none":
                raise Refused("conflict",
                              f"{key} is now review_tier=none")
            if int(fresh.get("round") or 0) != int(item.get("round") or 0):
                raise Refused("conflict",
                              f"{key} changed round under the transaction")
            if (fresh["claimed_by"] != item["claimed_by"]
                    or fresh["delegate"] != item["delegate"]):
                raise Refused("conflict",
                              f"{key} changed holder under the transaction")
            if self._verified_evidence_ids(fresh) != considered:
                raise Refused("conflict",
                              f"{key} evidence changed under the transaction")
            self._require_project(principal, fresh["project"])
            self._reviewer_family_ok(principal, fresh, self._ctx(fresh, now),
                                     change_request=not ok)
            prior = self._round_verdicts(fresh)
            if any(v.get("reviewer") == principal.name for v in prior):
                raise Refused("same_principal",
                              "reviewer already passed this round")
            tier = fresh.get("review_tier") or "standard"
            if tier == "standard":
                # D-228: ``_reviewer_family_ok`` above already proved this
                # reviewer is a distinct principal of a distinct, non-blank
                # family from the writer's, so one verdict closes the review.
                # Seat coverage is the closing gate: the sole standard seat
                # must cover every writer family of the item's commits.
                self._check_coverage(principal, fresh, prior, need=1,
                                     sensitive=False, now=now)
                self._append(principal, key, "review_verdict",
                             self._verdict_payload(principal, fresh), now)
                return self._pass(principal, key, reason, fresh, now,
                                  reviews_passed=1)
            # D-3084: seats cover PARTS.  A later seat may share a family with
            # an earlier one only under the waiver; without it the reviewer's
            # family must differ from EVERY prior seat's family, and a prior
            # seat with no family fails closed.
            if prior and not self._waiver_active(fresh):
                reviewer_family = auth.norm_family(principal.family)
                for v in prior:
                    seat_family = auth.norm_family(v.get("family"))
                    if seat_family is None:
                        raise Refused("family_unknown",
                                      "prior reviewer family unknown")
                    if reviewer_family == seat_family:
                        raise Refused("same_family",
                                      "reviewer shares a family with a prior seat")
            passed = len(prior) + 1
            if passed == 1:
                self._append(principal, key, "review_verdict",
                             self._verdict_payload(principal, fresh), now)
                return {"key": key, "state": "in_review",
                        "reviews_passed": passed}
            # From the second verdict on, a high round accumulates seats until
            # the per-part coverage (need=2, sensitive) holds.  A third-or-later
            # verdict is taken only while the seats so far still miss a writer
            # family; one arriving once they already cover is not needed and is
            # refused instead of appended.  A refusal appends nothing and
            # leaves the round intact.
            if len(prior) >= 2 and not self._missing_families(
                    None, fresh, prior, need=2, sensitive=True, now=now):
                raise Refused("conflict",
                              f"{key} seats already cover every writer")
            self._append(principal, key, "review_verdict",
                         self._verdict_payload(principal, fresh), now)
            missing = self._missing_families(principal, fresh, prior,
                                             need=2, sensitive=True, now=now)
            if not missing:
                return self._pass(principal, key, reason, fresh, now,
                                  reviews_passed=passed)
            return {"key": key, "state": "in_review", "reviews_passed": passed,
                    "missing": missing}

    def _missing_families(self, principal, item, prior, *, need, sensitive,
                          now):
        """Writer families the round's seats do not cover yet (D-3084).

        The single source of the seat-coverage math: ``coverage.uncovered``
        over the round's resolved seat families -- every ``prior`` verdict's
        family plus *principal*'s, or just ``prior`` when *principal* is
        ``None`` ("the seats so far").  Empty means the seats cover the item
        per part.  ``writer_families`` may raise ``writer_unknown``
        (fail closed).
        """
        from .coverage import uncovered, writer_families

        writers = writer_families(self, item, self._ctx(item, now))
        seats = [v.get("family") for v in prior]
        if principal is not None:
            seats.append(auth.norm_family(principal.family))
        return uncovered(writers, seats, need=need, sensitive=sensitive)

    def _check_coverage(self, principal, item, prior, *, need, sensitive, now):
        """Refuse a closing verdict whose writer families are not covered.

        Runs inside the verdict write lock, after every existing gate, on the
        verdict that would close the item only.  ``Refused('coverage', ...)``
        names the uncovered families and, because nothing has been appended
        yet, leaves the item's state, round and verdict history untouched.
        """
        missing = self._missing_families(principal, item, prior, need=need,
                                         sensitive=sensitive, now=now)
        if missing:
            raise Refused("coverage",
                          f"writer families not covered: {missing}")

    @staticmethod
    def _verdict_payload(principal, item):
        """The ``review_verdict`` event body for *principal* on *item*."""
        return {"reviewer": principal.name,
                "family": auth.norm_family(principal.family),
                "ok": True, "epoch": item["epoch"], "round": item["round"]}

    def _pass(self, principal, key, reason, item, now, reviews_passed):
        """Write ``done`` (and lock it) plus ``review_passed``, under the lock.

        Reached from either tier: one standard verdict or the second high
        verdict.  The UPDATE is guarded on ``in_review`` and the epoch, so a
        peer verdict that slipped in between the pre-read and here loses.
        """
        cursor = self.conn.execute(
            "UPDATE items SET state='done', reviewer=?, updated_at=?"
            " WHERE key=? AND state='in_review' AND epoch=?",
            (principal.name, self._iso(now), key, item["epoch"]))
        self._race_guard(cursor, key, "in_review", item["epoch"])
        self._append(principal, key, "review_passed", {"reason": reason}, now)
        return {"key": key, "state": "done", "reviews_passed": reviews_passed}

    def _verdict_changes(self, principal, key, reason, item, now):
        """``ok=False``: return to claimed (or open on a second change)."""
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != "in_review":
                raise Refused("conflict",
                              f"{key} is {fresh['state']}, not in review")
            if int(fresh["epoch"]) != int(item["epoch"]):
                raise Refused("conflict", f"{key} changed under the transaction")
            if (fresh.get("review_tier") or "standard") == "none":
                raise Refused("conflict",
                              f"{key} is now review_tier=none")
            if int(fresh.get("round") or 0) != int(item.get("round") or 0):
                raise Refused("conflict",
                              f"{key} changed round under the transaction")
            if (fresh["claimed_by"] != item["claimed_by"]
                    or fresh["delegate"] != item["delegate"]):
                raise Refused("conflict",
                              f"{key} changed holder under the transaction")
            # Mirror the ok=True path on the fresh row: a reviewer who was
            # re-scoped, delegated-over or made the claimant between the
            # pre-read and here must not be able to request changes either.
            if principal.name in (fresh["claimed_by"], fresh["delegate"]):
                raise Refused("same_principal", "reviewer is the claimant")
            self._require_project(principal, fresh["project"])
            # This branch is only ever reached with ``ok=False``.
            self._reviewer_family_ok(principal, fresh, self._ctx(fresh, now),
                                     change_request=True)
            prior = self._epoch_change_streak(fresh)
            second = prior >= 1
            state = "open" if second else "claimed"
            # A return to ``claimed`` is a fresh work window held by the same
            # claimant: give it a new default lease, recorded in the event so
            # ``events.fold`` and the cache agree, or the reaper (which skips
            # items with no lease) can never reclaim a dead claimant.
            lease = None if second else self._iso(now + self.default_lease)
            self._append(principal, key, "changes_requested",
                         {"state": state, "reason": reason,
                          "lease_until": lease, "epoch": fresh["epoch"],
                          "round": fresh["round"]}, now)
            if second:
                cursor = self.conn.execute(
                    "UPDATE items SET state='open', claimed_by=NULL, delegate=NULL,"
                    " reviewer=?, lease_until=NULL, updated_at=?"
                    " WHERE key=? AND state='in_review' AND epoch=?",
                    (principal.name, self._iso(now), key, item["epoch"]))
                self._finding(key, "changes_requested_twice",
                              "two reviews requested changes in a row", now)
            else:
                cursor = self.conn.execute(
                    "UPDATE items SET state='claimed', reviewer=?, lease_until=?,"
                    " updated_at=?"
                    " WHERE key=? AND state='in_review' AND epoch=?",
                    (principal.name, lease, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, "in_review", item["epoch"])
            # Both outcomes hand back new work, even when the claimant stays.
            self._exec_clear(self.conn, key, principal, now)
        return {"key": key, "state": state, "reviews_passed": 0}
