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
* ``claimed`` is refused as ``bad_state`` — the message naming the tier the row
  really carries (``'standard'``, ``'high'``, or ``NULL`` when it carries none;
  never a fabricated ``'standard'``) and pointing at ``in_review`` — whenever
  ``review_tier`` is not ``none`` *and* ``kind`` is not ``operator_task``.  On
  any other tier a legacy claim of a reviewed kind is a dead end: nobody holds
  the claimant token, so ``submit`` (claimant-only) cannot run, ``reap`` needs a
  lease, ``set_kind``/``set_legacy_state`` need ``open``, ``close_on_decision``
  refuses that kind at a tier above ``none`` and ``answer`` covers only
  ``question``/``decision``.  The ``operator_task`` exemption stands
  (KEEPER-OPCLAIM-CLOSE): that kind is never agent-reviewed, so
  ``close_on_decision`` closes it from ``claimed`` at *any* tier once the
  decision names the key in a whole ``Closes: <key>`` token (KEEPER-OPTASK-CLOSE,
  #129), i.e. the tier the exemption opens is a door to ``done``, never a dead
  end.  ``in_review`` stays allowed from ``open`` as before.
* recovery: ``in_review`` is also accepted from a row the pre-fix verb already
  stranded — and only where ``claimed`` itself is refused, i.e. a tier other
  than ``none`` on a kind other than ``operator_task``: ``claimed`` with
  ``lease_until`` NULL, ``claimed_by`` equal to the item's own ``owner``,
  ``delegate``/``reviewer``/``review_waiver`` all NULL and ``legacy_state_set``
  as its newest lifecycle event, i.e. a legacy claim nobody took over.  A row
  that ``close_on_decision`` can still reach from ``claimed`` is therefore
  *not* stranded and keeps ``bad_state`` — tier ``none`` for any kind, and
  ``operator_task`` at any tier, both of which close on a decision naming the
  key — as does any other ``claimed`` row (a real claim, a lease, a later
  claim event).
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
#: round) — and nothing else.  The newest of these on a key decides whether a
#: legacy claim is still the last word on the row: ``claimed``, ``released``,
#: ``lease_expired``, ``delegated``, ``heartbeat`` or ``submitted`` after the
#: ``legacy_state_set`` means somebody took the claim over, so the row is no
#: longer recoverable.  Metadata does not count — neither ``evidence_added``/
#: ``priority_set`` nor the definition events ``item_defined`` (a router edit
#: of tier/``done_criteria``/``depends_on``, even though it bumps the round and
#: sometimes the epoch) and ``kind_set`` (open rows only): an edit rewrites the
#: definition, it does not take over the claim.
_STATE_CHANGING = (
    "item_created", "claimed", "released",
    "lease_expired", "delegated", "heartbeat", "submitted",
    "changes_requested", "review_withdrawn", "item_reopened", "review_passed",
    "closed_on_decision", "answered", "cancelled", "state_set",
    "legacy_state_set",
)


def _tier_label(tier):
    """How a stored ``review_tier`` is named in a refusal.

    ``NULL`` is reported as itself: a row that never had a tier is refused on
    the same grounds as a standard one, but the message must not claim it is
    ``'standard'``.
    """
    return "NULL" if tier is None else repr(tier)


class LegacyMixin:
    """The router-only ``set_legacy_state`` verb."""

    def set_legacy_state(self, principal, key, state, note=""):
        return self._run(principal, "set_legacy_state", key,
                         lambda now: self._set_legacy_state(principal, key,
                                                            state, note, now))

    def _claimed_refusal(self, item):
        """``None`` when a legacy ``claimed`` move on *item* is allowed, else why.

        One predicate decides both the ``claimed`` target and the scope of the
        recovery (recovery only applies where ``claimed`` is refused), so the
        two can never drift apart: any tier other than ``none`` strands a
        legacy claim of a reviewed kind, while ``operator_task`` — like tier
        ``none`` — may sit in ``claimed`` because ``close_on_decision`` closes
        it from there (at any tier, on a decision that names the key), so
        neither of those shapes can strand.
        """
        tier = item.get("review_tier")
        if tier == "none" or item.get("kind") == "operator_task":
            return None
        return (f"{item['key']} is review_tier={_tier_label(tier)}: a legacy"
                " claim there strands the item (nobody holds the claimant"
                " token); use in_review")

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
        the item's own ``owner``, the holder/waiver columns the recovery write
        proves all NULL, and the newest lifecycle event the ``legacy_state_set``
        that put it there.  A real claim, a lease, a delegate/reviewer/waiver or
        any later lifecycle event (a router definition edit included — that
        rewrites the definition, it does not take the claim) makes it false, so
        those rows keep ``bad_state``.
        """
        owner = item.get("owner")
        return (item["state"] == "claimed"
                and item.get("lease_until") is None
                and bool(owner)
                and item.get("claimed_by") == owner
                and item.get("delegate") is None
                and item.get("reviewer") is None
                and item.get("review_waiver") is None
                and self._last_state_changing(item["key"]) == "legacy_state_set")

    def _set_legacy_state(self, principal, key, state, note, now):
        with self._write():
            self._require_role(principal, "router")
            if state not in _LEGACY_TARGETS:
                raise Refused("validation",
                              f"target state {state!r} must be claimed or"
                              " in_review")
            item = self._get_or_refuse(key)
            refusal = self._claimed_refusal(item)
            stranded = self._stranded_legacy_claim(item)
            recovered = False
            if item["state"] == "open":
                pass
            elif state == "in_review" and refusal is not None and stranded:
                # X7b's stranded row: recover it with the open->in_review
                # bookkeeping below, so the row folds back identically.
                recovered = True
            elif state == "in_review" and item["state"] == "claimed" \
                    and stranded:
                # The stranded *shape* on a row ``claimed`` is still allowed to
                # hold (tier ``none``, or an ``operator_task``): a legacy claim
                # there is closable, not dead-ended, because
                # ``close_on_decision`` fires from ``claimed`` — so recovery
                # deliberately does not reach it.
                raise Refused(
                    "bad_state",
                    f"{key} is claimed with"
                    f" review_tier={_tier_label(item.get('review_tier'))}"
                    f" and kind={item.get('kind')!r}: a legacy claim there is"
                    " closable (close_on_decision still fires from claimed),"
                    " not stranded, so it is not recovered")
            else:
                raise Refused("bad_state", f"{key} is {item['state']}, not open")
            if state == "claimed" and refusal is not None:
                raise Refused("bad_state", refusal)
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
                # Guarded like the open move, plus every column the recovery
                # itself proved: a claim, lease, delegate, reviewer, waiver or
                # round that slipped in under the lock keeps the old row and
                # loses the write.  ``round`` is compared NULL-safely (the
                # column is nullable on old rows) and the holder columns are
                # ``IS NULL``, never ``= NULL``, so the guard matches on
                # SQLite and PostgreSQL alike.
                round_value = item.get("round")
                cursor = self.conn.execute(
                    "UPDATE items SET state=?, epoch=?, round=?, claimed_by=?,"
                    " delegate=NULL, reviewer=NULL, lease_until=NULL,"
                    " review_waiver=NULL, updated_at=?"
                    " WHERE key=? AND state='claimed' AND epoch=?"
                    " AND claimed_by=? AND lease_until IS NULL"
                    " AND (round=? OR (round IS NULL AND ? IS NULL))"
                    " AND delegate IS NULL AND reviewer IS NULL"
                    " AND review_waiver IS NULL",
                    (state, epoch, round_no, owner, self._iso(now), key,
                     item["epoch"], owner, round_value, round_value))
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
