"""D-3066 router-only ``waive_floor`` verb for :class:`...store.Store`.

KEEPER-FLOOR-LEGACY-CLAIM: a legacy row — an old claim whose ``claimed``/
``released`` event carries no valid ``payload.base`` — can never close.
:meth:`store_support.SupportMixin._bind_floor` fails closed on an unrecorded
floor, so ``commit+ci`` refuses "no ancestry floor recorded ...", and the base
can never be re-derived after the fact.  The router may therefore *waive* the
ancestry floor for one item, by decision, so legacy rows can close.

Rules (all enforced under the single ``BEGIN IMMEDIATE``, like
``set_legacy_state`` — there is no external I/O to run outside the lock):

* router principal only — anything else is ``forbidden``;
* ``decision`` must match ``D-<ASCII digits>`` — anything else is
  ``validation``.  The id is recorded for **audit only**: unlike
  ``waive_second_family`` nothing is looked up through the ``decisions``
  adapter (no decision-scope token such as ``Waives:`` is honoured here), and
  the HTTP/MCP/CLI doors pass it through untouched;
* ``sha_floor`` is ``None`` (the floor requirement is waived outright) or a
  full lowercase commit sha that then *becomes* the floor — the descend check
  still runs against it.  Anything else is ``validation``;
* a **valid floor must not be recorded** for the item: when ``_bind_floor``
  answers a sha (``(base, True)`` — a release/foreign-claim base or a pinned
  ``sha_floor``), that provenance is not the router's to erase, so the verb
  refuses ``bad_state`` instead of waiving it.  The legacy shape
  (``(None, True)``) and a floor-less claim (``(None, False)``) may be
  waived — the latter pins a floor where none was required;
* the event payload records the claim ``epoch`` it was written for; a waiver
  for any other epoch is stale and ``_bind_floor`` ignores it.

The event is metadata only: no state, epoch, round, holder or token moves
(``floor_waived`` is in neither ``store_tokens._REVOKE_ALL`` nor
``token_fold.REVOKE_ALL`` and ``events.fold`` replays it as a no-op).  The
waiver is the newest word only until a later release or foreign claim
supersedes it by being newer: ``_bind_floor`` reads the newest of
released | foreign claimed | floor_waived, so the floor re-arms automatically.

PG parity (no separate PG path exists): the verb and ``_bind_floor`` run the
same SQL through the shared connection API — ``db_pg.PgConnection`` is a
sqlite3-compatible shim (db_pg.py) — and neither schema constrains the event
``type`` column (schema.py and schema_pg.py's ``events`` table both declare
``type TEXT NOT NULL`` with no CHECK; the ``EVENT_TYPES`` whitelist lives in
fold, one copy).  The floor tests run on Postgres unchanged via
``keeper_util.World`` following ``PG_KEEPER_BACKEND``.
"""
from __future__ import annotations

import re

from .errors import Refused
from .evidence_types import valid_commit_ref

#: The design's ``D-<digits>``: ASCII digits only.  ``\\d`` would also match
#: Unicode digits (e.g. Arabic-Indic ``D-٣٠٦٣``), which no audit trail wants;
#: ``fullmatch`` anchors both ends strictly (a trailing newline must not slip
#: past ``$``).
_DECISION_RE = re.compile(r"D-[0-9]+")


def _valid_sha_floor(value):
    """True only for a full lowercase hex sha — the shape ``_bind_floor`` trusts."""
    return valid_commit_ref(value) and value == value.lower()


class FloorMixin:
    """The router-only ``waive_floor`` verb."""

    def waive_floor(self, principal, key, decision, sha_floor=None):
        return self._run(principal, "waive_floor", key,
                         lambda now: self._waive_floor(principal, key,
                                                       decision, sha_floor,
                                                       now))

    def _waive_floor(self, principal, key, decision, sha_floor, now):
        with self._write():
            self._require_role(principal, "router")
            if not isinstance(decision, str) \
                    or _DECISION_RE.fullmatch(decision) is None:
                raise Refused("validation",
                              f"decision {decision!r} must match D-<digits>")
            if sha_floor is not None and not _valid_sha_floor(sha_floor):
                raise Refused("validation",
                              f"sha_floor {sha_floor!r} must be a full"
                              " lowercase commit sha")
            # The event is keyed to a real, unlocked item: the PG side rejects
            # event inserts for locked keys outright (``pgk_locked``), so
            # refuse here too instead of diverging between backends.
            item = self._get_or_refuse(key)
            self._assert_unlocked(item)
            # A recorded floor is provenance, not a legacy gap: waiving it
            # would erase a real descend anchor.  Only the floor-less shapes
            # (``(None, True)`` legacy / ``(None, False)`` first claim) may
            # be waived.
            floor_sha, _required = self._bind_floor(item)
            if floor_sha is not None:
                raise Refused("bad_state",
                              f"{key} has a recorded floor ({floor_sha[:12]});"
                              " refusing to waive it")
            epoch = item["epoch"]
            self._append(principal, key, "floor_waived",
                         {"decision": decision, "sha_floor": sha_floor or None,
                          "epoch": epoch},
                         now)
        return {"key": key, "decision": decision,
                "sha_floor": sha_floor or None}
