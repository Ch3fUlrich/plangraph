"""Review verbs for :class:`plangraph.keeper.store.Store`.

D-219: a change needs one implementer plus exactly **two** reviewers.  A
passing ``verdict(ok=True)`` records a ``review_verdict`` event and leaves the
item in review; only the second passing verdict from a *different principal* of
a *different family* re-verifies the evidence with the existing logic and emits
``review_passed`` (``done``).  A ``verdict(ok=False)`` from either seat returns
the item to ``claimed`` exactly as before and, because the next ``submit``
advances the submission ``round``, discards the round's earlier verdicts.

The waiver path (D-217) lives in ``store_waiver``: once the router records a
verified decision, ``_waiver_active`` is true and the second passing verdict may
share the first reviewer's family.
"""
from __future__ import annotations

import json

from .errors import Refused


class ReviewMixin:
    """The ``verdict`` verb and the two-reviewer bookkeeping."""

    # ----------------------------------------------------------- verdict
    def verdict(self, principal, key, ok, reason=""):
        return self._run(principal, "verdict", key,
                         lambda now: self._verdict(principal, key, ok, reason,
                                                   now))

    def _waiver_active(self, item):
        """D-217 hook: a verified waiver lets the two verdicts share a family."""
        return item.get("review_waiver") is not None

    def _round_verdicts(self, item):
        """Passing ``review_verdict`` payloads for this item's current round.

        Verdicts are tied to the submission round they judged: a changed verdict
        returns the item to ``claimed`` and the next ``submit`` bumps ``round``,
        so the earlier round's verdicts no longer match.
        """
        epoch, round_no = int(item["epoch"]), int(item.get("round") or 0)
        out = []
        for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='review_verdict'"
                " ORDER BY seq", (item["key"],)):
            payload = json.loads(row["payload"] or "{}")
            if (int(payload.get("epoch", -1)) == epoch
                    and int(payload.get("round", -1)) == round_no):
                out.append(payload)
        return out

    def _verdict(self, principal, key, ok, reason, now):
        self._require_role(principal, "reviewer")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "in_review":
            raise Refused("bad_state", f"{key} is {item['state']}, not in review")
        if principal.name in (item["claimed_by"], item["delegate"]):
            raise Refused("same_principal", "reviewer is the claimant")
        self._require_project(principal, item["project"])
        self._reviewer_family_ok(principal, item)
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
            if self._verified_evidence_ids(fresh) != considered:
                raise Refused("conflict",
                              f"{key} evidence changed under the transaction")
            self._require_project(principal, fresh["project"])
            self._reviewer_family_ok(principal, fresh)
            prior = self._round_verdicts(fresh)
            if any(v.get("reviewer") == principal.name for v in prior):
                raise Refused("same_principal",
                              "reviewer already passed this round")
            if len(prior) >= 2:
                raise Refused("conflict",
                              f"{key} already has two verdicts this round")
            if prior and not self._waiver_active(fresh):
                first_family = prior[0].get("family")
                if first_family is None:
                    raise Refused("family_unknown",
                                  "first reviewer family unknown")
                if principal.family == first_family:
                    raise Refused("same_family",
                                  "second reviewer shares a family")
            self._append(principal, key, "review_verdict",
                         {"reviewer": principal.name, "family": principal.family,
                          "ok": True, "epoch": fresh["epoch"],
                          "round": fresh["round"]}, now)
            passed = len(prior) + 1
            if passed >= 2:
                cursor = self.conn.execute(
                    "UPDATE items SET state='done', reviewer=?, updated_at=?"
                    " WHERE key=? AND state='in_review' AND epoch=?",
                    (principal.name, self._iso(now), key, item["epoch"]))
                self._race_guard(cursor, key, "in_review", item["epoch"])
                self._append(principal, key, "review_passed",
                             {"reason": reason}, now)
                return {"key": key, "state": "done", "reviews_passed": passed}
        return {"key": key, "state": "in_review", "reviews_passed": passed}

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
            # Mirror the ok=True path on the fresh row: a reviewer who was
            # re-scoped, delegated-over or made the claimant between the
            # pre-read and here must not be able to request changes either.
            if principal.name in (fresh["claimed_by"], fresh["delegate"]):
                raise Refused("same_principal", "reviewer is the claimant")
            self._require_project(principal, fresh["project"])
            self._reviewer_family_ok(principal, fresh)
            prior = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE key=?"
                " AND type='changes_requested'", (key,)).fetchone()[0]
            second = prior >= 1
            state = "open" if second else "claimed"
            # A return to ``claimed`` is a fresh work window held by the same
            # claimant: give it a new default lease, recorded in the event so
            # ``events.fold`` and the cache agree, or the reaper (which skips
            # items with no lease) can never reclaim a dead claimant.
            lease = None if second else self._iso(now + self.default_lease)
            self._append(principal, key, "changes_requested",
                         {"state": state, "reason": reason,
                          "lease_until": lease}, now)
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
        return {"key": key, "state": state, "reviews_passed": 0}
