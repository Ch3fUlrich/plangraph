"""Router review decisions: D-217 waiver and D-5504/D-5506 merged GO close.

The two-reviewer gate (D-219, ``store_review``) normally requires the second
passing verdict to come from a different family.  When that seat is unavailable
the router may record a **verified ``decision``** that waives the second-family
requirement for one item: after ``waive_second_family(principal, key, D-NNN)``
the two passing verdicts may share a family.  Distinct principals, non-null
families and the claimant/delegate exclusion all still apply — the waiver only
relaxes *which families* count.

Like ``add_evidence_router`` the slow decision verification runs before the
write lock, and the row is re-read and guarded inside ``BEGIN IMMEDIATE``.

Only a ``high`` item has a second family to waive (D-228): waiving a
``standard`` or ``none`` item is refused ``bad_state``.

The decision does not waive on its own: its D-row must carry the whole token
``Waives: <key>`` for the item being waived (X6c3), checked through the
``decisions`` adapter in that same pre-lock I/O.
"""
from __future__ import annotations

import json
import re

from . import auth, context_sources
from .coverage import writer_families
from .errors import Refused
from .evidence_types import Result, valid_commit_ref
from ..review.family import model_family


class WaiverMixin:
    """Router decision verbs for reviews: waive a family or close merged work."""

    def close_merged(self, principal, key, decision_id, families):
        return self._run(principal, "close_merged", key,
                         lambda now: self._close_merged(principal, key, decision_id,
                                                       families, now))

    @staticmethod
    def _merged_review_state(item):
        if item["state"] != "in_review":
            raise Refused("bad_state", f"{item['key']} is {item['state']}, not in_review")
        if item.get("review_tier") not in ("standard", "high"):
            raise Refused("bad_state", "close_merged requires review_tier=standard or high")

    def _merged_go_proof(self, item, decision_id, now):
        commits = context_sources.plan_item_commits(
            self._adapter("git"), item["project"], item["key"], fetch=False)
        head = commits[0].get("sha") if commits else None
        if not valid_commit_ref(head):
            raise Refused("evidence_unverified", "merged_on_default_missing")
        result = self._verify_evidence(item, "decision", {"id": decision_id}, now)
        if not result.ok:
            raise Refused("evidence_unverified", result.detail)
        try:
            text = self._ctx(item, now).decisions.text(decision_id)
        except Exception:  # noqa: BLE001 - absent/unreadable row text never authorizes
            text = None
        if not isinstance(text, str) or not re.search(r"(?<![\w-])GO(?![\w-])", text) \
                or re.search(r"(?i)\b(?:no|not)[\s-]+go\b", text):
            raise Refused("evidence_unverified", "go_row_not_go")
        tokens = re.findall(r"(?<![\w])[0-9a-fA-F]{8,64}(?![\w])", text)
        if not any(head.startswith(token.lower()) for token in tokens):
            raise Refused("evidence_unverified", "go_row_head_mismatch")
        return head, result

    def _merged_seats(self, item, families, now):
        if not isinstance(families, list):
            raise Refused("validation", "families must be a list")
        seats = []
        for value in families:
            family = auth.norm_family(value)
            if family is None:
                raise Refused("family_unknown", "seat_family_empty")
            # Resolve model aliases too: Claude/Anthropic may not masquerade as
            # independent families through different provider/model spellings.
            family = model_family(family) or auth.validate_family(family)
            if family == "anthropic":
                raise Refused("same_family", "anthropic_seat_family")
            if family in seats:
                raise Refused("same_family", "duplicate_seat_family")
            seats.append(family)
        need = 2 if item["review_tier"] == "high" else 1
        if len(seats) < need:
            raise Refused("coverage", f"insufficient_seat_families: need {need}")
        writers = writer_families(self, item, self._ctx(item, now))
        writers.append(item.get("owner_family"))
        if item.get("writer_model"):
            family = model_family(item["writer_model"])
            if family is None:
                raise Refused("writer_unknown", "recorded writer model family unknown")
            writers.append(family)
        for name in (item.get("claimed_by"), item.get("delegate")):
            peer = auth.get_principal(self.conn, name)
            if peer is not None:
                writers.append(peer.family)
        for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='run_recorded'",
                (item["key"],)):
            writer = json.loads(row["payload"]).get("writer") or {}
            family = writer.get("family") or model_family(writer.get("model"))
            if writer and not family:
                raise Refused("writer_unknown", "recorded run writer family unknown")
            writers.append(family)
            writers.append(model_family(writer.get("model")))
        writers = {model_family(w) or auth.norm_family(w) for w in writers}
        if writers.intersection(seats):
            raise Refused("same_family", "writer_seat_family")
        return seats

    def _close_merged(self, principal, key, decision_id, families, now):
        self._require_role(principal, "router")
        item = self._get_or_refuse(key)
        self._merged_review_state(item)  # DONE is bad_state, not locked
        self._assert_unlocked(item)
        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "decision_id must look like D-NNN")
        head, _ = self._merged_go_proof(item, decision_id, now)
        seats = self._merged_seats(item, families, now)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._merged_review_state(fresh)
            self._assert_unlocked(fresh)
            if fresh != item:
                raise Refused("conflict", f"{key} changed under the transaction")
            current_head, result = self._merged_go_proof(fresh, decision_id, now)
            if current_head != head:
                raise Refused("conflict", "merged_on_default_changed")
            seats = self._merged_seats(fresh, seats, now)
            ref = {"id": decision_id, "families": seats,
                   "merged_head": head, "caller": principal.name}
            self._record_evidence(principal, fresh, "decision", ref, now,
                                  Result(True, result.detail, ref))
            return self._pass(principal, key, f"router GO {decision_id} merged {head}",
                              fresh, now, reviews_passed=len(seats))

    def waive_second_family(self, principal, key, decision_id):
        return self._run(principal, "waive_second_family", key,
                         lambda now: self._waive(principal, key, decision_id,
                                                 now))

    def _waive(self, principal, key, decision_id, now):
        self._require_role(principal, "router")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}, not active")
        if (item.get("review_tier") or "standard") in ("standard", "none"):
            # D-228: a standard item passes on one cross-family verdict, and a
            # none item never takes verdicts at all, so neither has a second
            # family seat to waive.
            raise Refused("bad_state",
                          f"{key} is review_tier={item.get('review_tier')}:"
                          " no second family to waive")
        if not isinstance(decision_id, str) or not decision_id:
            raise Refused("validation", "decision_id is required")
        # The slow decision lookup runs before the write lock, exactly as
        # ``add_evidence_router`` verifies its evidence.
        result = self._verify_evidence(item, "decision", {"id": decision_id}, now)
        if not result.ok:
            raise Refused("evidence_unverified",
                          result.detail or f"decision {decision_id} unverified")
        # X6c3: the recorded decision must name this item as its waiver scope
        # (``Waives: <project:ID>`` in its D-row).  Also pre-lock read-only I/O.
        decisions = self._ctx(item, now).decisions
        waives = getattr(decisions, "waives", None)
        names = [key, *self.alias_sources(key)]  # a rehomed item's old keys
        if not callable(waives) or not any(waives(decision_id, k) for k in names):
            raise Refused("evidence_unverified",
                          f"{decision_id} does not name {key} (Waives:)")
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] not in ("claimed", "in_review"):
                raise Refused("conflict", f"{key} is {fresh['state']}")
            if (fresh.get("review_tier") or "standard") in ("standard", "none"):
                raise Refused("conflict", f"{key} is no longer review_tier=high")
            cursor = self.conn.execute(
                "UPDATE items SET review_waiver=?, updated_at=?"
                " WHERE key=? AND epoch=?"
                " AND state IN ('claimed','in_review')",
                (decision_id, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, fresh["state"], item["epoch"])
            self._append(principal, key, "waiver_set",
                         {"decision_id": decision_id}, now)
            return {"key": key, "state": fresh["state"],
                    "review_waiver": decision_id}
