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
* ``decision`` must match ``^D-\\d+$`` — anything else is ``validation``.
  The id is recorded for audit only in this slice: unlike
  ``waive_second_family`` nothing is looked up through the ``decisions``
  adapter (slice 2 wires the API/MCP/CLI doors);
* ``sha_floor`` is ``None`` (the floor requirement is waived outright) or a
  full lowercase commit sha that then *becomes* the floor — the descend check
  still runs against it.  Anything else is ``validation``.

The event is metadata only: no state, epoch, round, holder or token moves
(``floor_waived`` is in neither ``store_tokens._REVOKE_ALL`` nor
``token_fold.REVOKE_ALL`` and ``events.fold`` replays it as a no-op).  The
waiver is the newest word only until a later release or foreign claim
supersedes it by being newer: ``_bind_floor`` reads the newest of
released | foreign claimed | floor_waived, so the floor re-arms automatically.
"""
from __future__ import annotations

import re

from .errors import Refused
from .evidence_types import valid_commit_ref

#: The design's ``^D-\\d+$``; ``fullmatch`` anchors both ends strictly (a
#: trailing newline must not slip past ``$``).
_DECISION_RE = re.compile(r"D-\d+")


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
            self._assert_unlocked(self._get_or_refuse(key))
            self._append(principal, key, "floor_waived",
                         {"decision": decision, "sha_floor": sha_floor or None},
                         now)
        return {"key": key, "decision": decision,
                "sha_floor": sha_floor or None}
