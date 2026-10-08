"""X7c router-only ``set_legacy_state`` verb for :class:`...store.Store`.

A legacy row imported or defined while ``open`` has no claimant and never
entered the review pipeline.  The router may move such an item straight into an
active state so that ``close_on_decision`` (which only fires from
``claimed``/``in_review``) can then close it.  The verb is the router's only way
to open an ``open`` item without a real claim, and it honours *no* external I/O:
there is nothing to verify, so unlike ``waive_second_family`` or
``add_evidence_router`` every check runs inside the single ``BEGIN IMMEDIATE``.

Rules (all enforced under the write lock):

* router principal only — anything else is ``forbidden``;
* the target state must be ``claimed`` or ``in_review`` — any other value,
  including ``done``, is ``validation``;
* the item must currently be ``open`` — a non-open (or terminal) item is
  ``bad_state``.  Because an open item can never carry a ``review_passed`` or
  ``closed_on_decision`` event, this check also subsumes the locked case.
* the move opens a fresh work epoch (``epoch + 1``) at a fresh ``round`` and
  clears ``delegate``/``reviewer``/``lease_until``/``review_waiver``, so
  earlier rounds' verdicts never fill a review seat and the lease reaper
  (which only expires claimed items *with* a lease) never reopens the claim.
  ``done`` is then reachable only through the normal tier rule: the tier's
  reviewer count of distinct non-claimant families plus verified evidence.

``claimed_by`` becomes the item's own ``owner`` string, and the
``legacy_state_set`` event carries that owner so ``fold`` rebuilds the cached
row exactly and ``verify_integrity`` stays silent.
"""
from __future__ import annotations

from .errors import Refused

_LEGACY_TARGETS = ("claimed", "in_review")


class LegacyMixin:
    """The router-only ``set_legacy_state`` verb."""

    def set_legacy_state(self, principal, key, state, note=""):
        return self._run(principal, "set_legacy_state", key,
                         lambda now: self._set_legacy_state(principal, key,
                                                            state, note, now))

    def _set_legacy_state(self, principal, key, state, note, now):
        with self._write():
            self._require_role(principal, "router")
            if state not in _LEGACY_TARGETS:
                raise Refused("validation",
                              f"target state {state!r} must be claimed or"
                              " in_review")
            item = self._get_or_refuse(key)
            if item["state"] != "open":
                raise Refused("bad_state", f"{key} is {item['state']}, not open")
            self._assert_unlocked(item)
            owner = item.get("owner")
            # A fresh work epoch with a fresh round: earlier rounds' verdicts
            # no longer match ``_round_verdicts`` and the cleared holder
            # columns agree with the fold.  The lease stays NULL so the
            # reaper (which only expires claimed items *with* a lease) never
            # reopens a legacy claim; a waiver belonged to a dead epoch.
            epoch = int(item["epoch"]) + 1
            round_no = int(item.get("round") or 0) + 1
            self._append(principal, key, "legacy_state_set",
                         {"state": state, "note": note or "", "legacy": True,
                          "claimed_by": owner, "epoch": epoch,
                          "round": round_no}, now)
            cursor = self.conn.execute(
                "UPDATE items SET state=?, epoch=?, round=?, claimed_by=?,"
                " delegate=NULL, reviewer=NULL, lease_until=NULL,"
                " review_waiver=NULL, updated_at=?"
                " WHERE key=? AND state='open' AND epoch=?",
                (state, epoch, round_no, owner, self._iso(now), key,
                 item["epoch"]))
            self._race_guard(cursor, key, "open", item["epoch"])
        return {"key": key, "state": state, "claimed_by": owner}
