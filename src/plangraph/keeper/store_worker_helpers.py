"""Worker ownership and state guards."""
from .errors import Refused
import json
from .store_cards import _exec_parts, SETTABLE_STATES

_LIVE_SET = frozenset(("open", "ready", "running", "waiting", "needs_input"))
_AWAY_STATES = frozenset(("waiting", "needs_input"))
_EXEC_IDS = ("workflow_id", "run_id", "attempt")


class WorkerGuardsMixin:
    def _system_phase_holder(self, principal, item):
        """Recognize only the system phase-worker claim door, not plain claims."""
        if not (principal is not None and principal.role in ("system", "phase-worker")
                and (principal.name == "system" or getattr(principal, "worker_scope", False)) and item["kind"] == "lane"
                and item["state"] == "claimed"
                and item["claimed_by"] == principal.name):
            return False
        claim = None
        for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='claimed'"
                " ORDER BY seq DESC", (item["key"],)):
            payload = json.loads(row["payload"] or "{}")
            if payload.get("epoch") == item["epoch"]:
                claim = payload
                break
        if not claim or claim.get("via") != "worker_claim":
            return False
        return self.conn.execute(
            "SELECT 1 FROM edges e JOIN items i ON i.key=e.src"
            " WHERE e.dst=? AND i.kind='gate' AND i.state!='cancelled'"
            " LIMIT 1", (item["key"],)).fetchone() is not None

    def _exec_expect_state(self, item, expected, principal=None, *, now=None,
                           claim_checked=False, park=False) -> None:
        """Check observed state inside the write lock, before recording ownership.

        ``park=True`` adds the EXEC-OBSERVED-STATE rule: a park closes the card,
        so onto a state a projector event wrote while this run was away
        (``waiting``, ``needs_input``) the run must present that state as an
        observation -- the live set, which only says "I observed nothing", does
        not stand in for one.
        """
        if expected is None:
            return
        states = [expected] if isinstance(expected, str) else expected
        if (not isinstance(states, list) or not states
                or any(not isinstance(s, str) or s not in SETTABLE_STATES
                       for s in states)):
            raise Refused("validation",
                          f"expected_state must be a state or non-empty list from {list(SETTABLE_STATES)}")
        live_set = (isinstance(expected, list)
                    and set(states) == _LIVE_SET)
        phase_holder = (live_set
                        and item["state"] == "claimed"
                        and principal is not None
                        and (principal.role == "worker"
                             or self._system_phase_holder(principal, item))
                        and item["claimed_by"] == principal.name)
        if phase_holder and not claim_checked:
            # The live-set exception must not revive a dead claim on ask or
            # complete; park already enforces this same lease rule.
            self._park_guard(principal, item, None,
                             self.clock() if now is None else now)
        if park and live_set and item["state"] in _AWAY_STATES:
            raise Refused("exec_stale_state",
                          f"{item['key']}: the live set is not an observation,"
                          f" is {item['state']!r}")
        if item["state"] not in states and not phase_holder:
            raise Refused("exec_stale_state",
                          f"{item['key']}: expected {expected!r}, is {item['state']!r}")

    def _resolve_scoped(self, principal, card):
        """Resolve *card* to its live key, refuse if unknown, check scope."""
        key = self.resolve_key(card)
        item = self._get_or_refuse(key)
        self._may_act_in(principal, item["project"])
        return key

    def _park_guard(self, principal, item, epoch, now):
        """Bind a park to the card's current claim (same rules as heartbeat/submit).

        Checked, inside the write lock, for workers and verified system phase
        holders; other router/system calls retain their administrative bypass:
        * a card with no claim (no ``claimed_by``: ``running`` set via set_state)
          has no holder to bind: only project scope applies;
        * otherwise the principal name must be the card's ``claimed_by`` or
          ``delegate`` (``_actor_or_delegate``, 403 forbidden): a worker whose
          claim was released and re-taken by another principal is refused;
        * if the body carried ``epoch`` it must equal the card's epoch
          (``_epoch_ok``, 409 epoch_stale) -- the Rust ParkPacket has no epoch,
          so the field is optional and holder + lease are the binding it has;
        * an expired lease (``lease_until`` at or before now) is refused as
          409 epoch_stale: the claim is dead and the reaper will reopen the card.
        """
        if (principal.role in ("router", "system")
                and not self._system_phase_holder(principal, item)) or not item["claimed_by"]:
            return
        self._actor_or_delegate(principal, item)
        if epoch is not None:
            self._epoch_ok(item, epoch)
        due = self._parse_iso(item["lease_until"]) if item["lease_until"] else None
        if due is not None and due.timestamp() <= now:
            raise Refused("epoch_stale", f"{item['key']}: claim lease expired")

    def _exec_gate(self, key, body, principal=None, *, record=True):
        """Check a worker verb body against the card's recorded exec owner.

        Recording runs INSIDE the caller's write lock (park's ``guard`` hook and
        complete/alarm's write block) because it may record the owner or bump
        the attempt.  RT-P1-EXEC-OWNERSHIP: a body naming any of
        ``workflow_id``/``run_id``/``attempt`` must present all three and match
        the owner (``_exec_check``: a first call records it -- from a worker
        principal only, bound to it -- a stale run, a retired run or a lower
        attempt is refused); a body without them keeps the historical
        behaviour unless the card is already owned. ``record=False`` validates
        without acquiring ownership (audit-only or no-op) -- it still runs the
        stale and retired-run fence checks, so a late call from a retired run
        can never slip past a validate-only call, and permits a plain read for
        audit-only attempts.
        """
        row = self.conn.execute(
            "SELECT exec_run_id FROM items WHERE key=?", (key,)).fetchone()
        owned = row is not None and row["exec_run_id"] is not None
        if not any(body.get(name) is not None
                   for name in ("workflow_id", "run_id", "attempt")):
            if owned:
                raise Refused("exec_stale",
                              f"{key} has an exec owner: workflow_id, run_id"
                              " and attempt are required")
            return
        parts = _exec_parts(key, (body.get("workflow_id"), body.get("run_id"),
                                  body.get("attempt")))
        self._exec_check(self.conn, key, *parts, principal=principal, record=record)

    @staticmethod
    def _exec_duplicate(item, body, final):
        """True when *body* retries a worker call that already reached *final*.

        RT-P1-EXEC-OWNERSHIP: a park whose response was lost (card already
        ``parked``) or a complete whose response was lost (card already
        ``done``) is a SUCCESS NO-OP, decided BEFORE ``expected_state``: the
        observed state the retry carries is the state the worker saw before its
        first call succeeded, so honouring it would turn a duplicate into
        ``exec_stale_state`` on every retry.  It is a duplicate only when the
        body's ``(workflow_id, run_id, attempt)`` equals the recorded owner, or
        the card has no owner and the body carries no triple -- a different run,
        a backwards attempt and a malformed triple are refused by the caller's
        stale/lower-attempt check, which runs first.
        """
        if item["state"] != final:
            return False
        parts = tuple(body.get(name) for name in _EXEC_IDS)
        if all(part is None for part in parts):   # no triple: as the gate reads it
            return item["exec_run_id"] is None
        return parts == (item["exec_workflow_id"], item["exec_run_id"],
                         item["exec_attempt"])

    # ----------------------------------------------------------- attempts
    # -------------------------------------------------------- jev outcome
    def _fill_jev_outcome(self, principal, card, outcome, *, attempt=None):
        """Fill the outcome of this card's Jev rows; observe-only, never raises.

        RT-P1-JEV-SHADOW: ``worker_park`` and ``worker_complete`` call this AFTER
        their own write transaction has committed and outside it, because the fill
        is a separate write. Numbered attempt notifications fill only that
        attempt's routing kinds; legacy park/complete notifications without a
        number fill only NULL-attempt routing rows (never all attempts), with
        the same ``exit_class``/``outcome`` text
        :func:`plangraph.planner.shadow.record_outcome` derives for the planner.

        The row is filed by :func:`plangraph.keeper.store_jev._jev_writer`, the
        same mapping the verdict's ``qc_accept`` row uses: the caller when its
        role may write ``jev_shadow`` (router/system/l1) and the synthetic
        ``system`` identity otherwise.  That matters here because the real
        attempt-finish path is a ``worker`` (or ``spawner``) - a role
        ``store_jev._WRITE_ROLES`` refuses - so testing the role first, as this
        once did, meant the outcome was never filled on the path it exists for;
        the mapping instead lands every fill on a principal that may write, so
        it can never become a swallowed refusal, for any role.

        A card with no rows is still skipped before any transaction is opened -
        an outcome only ever fills rows that exist, so a keeper that never ran
        Jev keeps its audit log and its write lock exactly as they were.
        Everything (import, refusal, store error) is swallowed: a Jev outcome is
        best-effort and can never fail or delay a worker verb.
        """
        if not card:
            return
        try:
            # Late imports: the planner and the Jev mixin are only needed when a
            # fill can actually happen (``store_jev`` imports this module).
            from .store_jev import _jev_writer
            if self.conn.execute(
                    "SELECT 1 FROM jev_shadow WHERE key=? LIMIT 1",
                    (card,)).fetchone() is None:
                return
            from plangraph.planner import shadow as shadow_mod
            shadow_mod.record_outcome(None, outcome, store=self,
                                      principal=_jev_writer(principal),
                                      card_key=card, attempt=attempt)
        except Exception:  # noqa: BLE001 - a Jev outcome never breaks a worker verb
            pass
