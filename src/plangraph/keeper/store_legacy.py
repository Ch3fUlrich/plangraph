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
  ``closed_on_decision`` event, this check also subsumes the locked case.  The
  one exception is recovery (below): an item already stranded in ``claimed``
  may move to ``in_review``.
* ``claimed`` is refused as ``bad_state`` — the message naming the tier and
  pointing at ``in_review`` — whenever the item's ``review_tier`` is not
  ``none`` and its ``kind`` is not ``operator_task``.  On any other tier a
  legacy claim is a dead end: nobody holds the claimant token, so ``submit``
  (claimant-only) cannot run, ``reap`` needs a lease, ``set_kind``/
  ``set_legacy_state`` need ``open`` and ``close_on_decision`` needs
  ``review_tier='none'``.  ``in_review`` stays allowed from ``open`` as before.
* recovery: ``in_review`` is also accepted from a row the pre-fix verb already
  stranded — ``claimed`` with ``lease_until`` NULL, ``claimed_by`` equal to the
  item's own ``owner`` and ``legacy_state_set`` as its newest state-changing
  event, i.e. a legacy claim nobody took over.  Any other ``claimed`` item (a
  real claim, a lease, a later claim event) keeps ``bad_state``.
* the move opens a fresh work epoch (``epoch + 1``) at a fresh ``round`` and
  clears ``delegate``/``reviewer``/``lease_until``/``review_waiver``, so
  earlier rounds' verdicts never fill a review seat and the lease reaper
  (which only expires claimed items *with* a lease) never reopens the claim.
  ``done`` is then reachable only through the normal tier rule: the tier's
  reviewer count of distinct non-claimant families plus verified evidence.

``claimed_by`` becomes the item's own ``owner`` string, and the
``legacy_state_set`` event carries that owner so ``fold`` rebuilds the cached
row exactly and ``verify_integrity`` stays silent — on both the ``open`` move
and the recovery.
"""
from __future__ import annotations

from .errors import Refused

_LEGACY_TARGETS = ("claimed", "in_review")

#: Event types that move an item's lifecycle (state, holder, lease, epoch or
#: round).  The newest of these on a key decides whether a legacy claim is
#: still the last word on the row; ``evidence_added``/``priority_set`` and the
#: other metadata events do not count, but a ``claimed``, ``delegated``,
#: ``heartbeat`` or ``submitted`` after the ``legacy_state_set`` means somebody
#: took the claim over, so the row is no longer recoverable.
_STATE_CHANGING = (
    "item_created", "item_defined", "kind_set", "claimed", "released",
    "lease_expired", "delegated", "heartbeat", "submitted",
    "changes_requested", "review_withdrawn", "item_reopened", "review_passed",
    "closed_on_decision", "answered", "cancelled", "state_set",
    "legacy_state_set",
)


class LegacyMixin:
    """The router-only ``set_legacy_state`` verb."""

    def set_legacy_state(self, principal, key, state, note=""):
        return self._run(principal, "set_legacy_state", key,
                         lambda now: self._set_legacy_state(principal, key,
                                                            state, note, now))

    def _last_state_changing(self, key):
        """The type of the newest lifecycle event on *key*, or ``None``."""
        placeholders = ",".join("?" for _ in _STATE_CHANGING)
        row = self.conn.execute(
            f"SELECT type FROM events WHERE key=? AND type IN ({placeholders})"
            " ORDER BY seq DESC LIMIT 1",
            (key, *_STATE_CHANGING)).fetchone()
        return row["type"] if row is not None else None

    def _stranded_legacy_claim(self, item):
        """Is *item* the dead-end row the pre-fix ``claimed`` move wrote?

        True only for a legacy claim nobody took over: no lease, still held by
        the item's own ``owner``, and the newest state-changing event is the
        ``legacy_state_set`` that put it there.  A real claim, a lease or any
        later lifecycle event makes it false, so those rows keep ``bad_state``.
        """
        owner = item.get("owner")
        return (item["state"] == "claimed"
                and item.get("lease_until") is None
                and bool(owner)
                and item.get("claimed_by") == owner
                and self._last_state_changing(item["key"]) == "legacy_state_set")

    def _set_legacy_state(self, principal, key, state, note, now):
        with self._write():
            self._require_role(principal, "router")
            if state not in _LEGACY_TARGETS:
                raise Refused("validation",
                              f"target state {state!r} must be claimed or"
                              " in_review")
            item = self._get_or_refuse(key)
            recovered = False
            if item["state"] == "open":
                pass
            elif state == "in_review" and self._stranded_legacy_claim(item):
                # X7b's stranded row: recover it with the open->in_review
                # bookkeeping below, so the row folds back identically.
                recovered = True
            else:
                raise Refused("bad_state", f"{key} is {item['state']}, not open")
            if state == "claimed":
                tier = item.get("review_tier") or "standard"
                if tier != "none" and item.get("kind") != "operator_task":
                    raise Refused(
                        "bad_state",
                        f"{key} is review_tier={tier!r}: a legacy claim there"
                        " strands the item (nobody holds the claimant token);"
                        " use in_review")
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
            if recovered:
                # Guarded like the open move, plus the stranded-row columns the
                # recovery itself proved: a claim or lease that slipped in
                # under the lock keeps the old row and loses the write.
                cursor = self.conn.execute(
                    "UPDATE items SET state=?, epoch=?, round=?, claimed_by=?,"
                    " delegate=NULL, reviewer=NULL, lease_until=NULL,"
                    " review_waiver=NULL, updated_at=?"
                    " WHERE key=? AND state='claimed' AND epoch=?"
                    " AND claimed_by=? AND lease_until IS NULL",
                    (state, epoch, round_no, owner, self._iso(now), key,
                     item["epoch"], owner))
            else:
                cursor = self.conn.execute(
                    "UPDATE items SET state=?, epoch=?, round=?, claimed_by=?,"
                    " delegate=NULL, reviewer=NULL, lease_until=NULL,"
                    " review_waiver=NULL, updated_at=?"
                    " WHERE key=? AND state='open' AND epoch=?",
                    (state, epoch, round_no, owner, self._iso(now), key,
                     item["epoch"]))
            self._race_guard(cursor, key, item["state"], item["epoch"])
        return {"key": key, "state": state, "claimed_by": owner}
