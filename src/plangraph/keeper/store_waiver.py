"""Router review decisions: D-217 waiver and D-5504/D-5506 merged GO close.

The two-reviewer gate (D-219, ``store_review``) normally requires two passing
verdicts from different families. When that seat is unavailable, the router may
record a **verified ``decision``** for one item using
``waive_second_family(principal, key, D-NNN)``. An active D-id waiver permits
one passing verdict with single-seat coverage (D-5658), except auth/secrets
cards still require two distinct non-Anthropic families. Distinct principals,
non-null families, writer-family conflicts and claimant/delegate exclusions
still apply; the waiver itself is not a verdict.

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
from .schema import RISK_TAGS
from ..review.family import FAMILIES, model_family


def _known_family(value):
    family = auth.norm_family(value)
    return family if family in {f for _, f in FAMILIES} else model_family(family)


class WaiverMixin:
    """Router decision verbs for reviews: waive a family or close merged work."""

    def close_merged(self, principal, key, decision_id, families=None, *, from_open=False, apply=False):
        audit_key = key if isinstance(key, str) else None
        return self._run(principal, "close_merged", audit_key,
                         lambda now: self._close_merged(principal, key, decision_id,
                                                       families, now, from_open, apply))

    @staticmethod
    def _merged_review_state(item, from_open=False):
        if item["state"] not in (("open", "claimed") if from_open else ("in_review",)):
            raise Refused("bad_state", f"{item['key']} is {item['state']}, not in_review")
        if item.get("review_tier") not in ("standard", "high"):
            raise Refused("bad_state", "close_merged requires review_tier=standard or high")

    def _merged_go_proof(self, item, decision_id, now, *, all_work=False):
        git = self._adapter("git")
        try:
            commits = context_sources.plan_item_commits(
                git, item["project"], item["key"], fetch=False,
                **({"limit": None} if all_work else {}))
        except Exception:  # noqa: BLE001 - an unreadable git proof never authorizes
            commits = None
        heads = set()
        # A trailer-bearing commit may be inside a multi-commit PR, or a later
        # direct follow-up. Bind to the introducing merge's second parent only.
        for commit in commits or ():
            sha = commit.get("sha")
            if not valid_commit_ref(sha):
                continue
            try:
                repo, ref = git.repo_path(item["project"]), git.repo_ref(item["project"])
                merge = git.merge_commit_of(repo, sha, ref)
                if not merge or not valid_commit_ref(merge[0]):
                    continue
                proc = git._run(repo, ["show", "-s", "--format=%P",
                                       "--end-of-options", merge[0]])
                parents = proc.stdout.strip().split() if proc.returncode == 0 else []
                if len(parents) == 2 and all(valid_commit_ref(p) for p in parents):
                    heads.add(parents[1])
            except Exception:  # noqa: BLE001 - an unknown merge never authorizes
                continue
        if not heads:
            raise Refused("evidence_unverified", "merged_on_default_missing")
        result = self._verify_evidence(item, "decision", {"id": decision_id}, now)
        if not result.ok:
            raise Refused("evidence_unverified", result.detail)
        try:
            text = self._ctx(item, now).decisions.text(decision_id)
        except Exception:  # noqa: BLE001 - absent/unreadable row text never authorizes
            text = None
        # DecisionsAdapter.text returns ONLY the DECISION cell (D-276).
        # Only the router's timestamp prefix may precede the GO verdict.
        if isinstance(text, str):
            text = re.sub(r"^\(~?(?:[01][0-9]|2[0-3]):[0-5][0-9x]Z"
                          r"(?:, date -u)?\)\s+", "", text)
        if not isinstance(text, str) or not re.match(r"^GO(?=$|\s)", text):
            raise Refused("evidence_unverified", "go_row_not_go")
        if re.match(r"^GO\s+(?:#[0-9]+|`#[0-9]+`)(?=$|[\s,;:.])", text):
            # Review prose may itself discuss a NO GO; authority is the leading
            # GO verdict and its one explicit, full-length GO-HEAD token.
            bounds = re.findall(r"(?<![\w-])GO-HEAD\s+`?([0-9a-fA-F]{40})(?![\w])", text)
        else:
            if re.search(r"(?i)\b(?:no|not)\b.*\bgo\b", text):
                raise Refused("evidence_unverified", "go_row_not_go")
            bound = re.match(r"^GO\s+`?([0-9a-fA-F]{8,64})(?![\w])", text)
            bounds = [bound[1]] if bound else []
        matched = {head for head in heads if len(bounds) == 1
                   and head.startswith(bounds[0].lower())}
        if len(matched) != 1:
            raise Refused("evidence_unverified", "go_row_head_mismatch")
        return matched.pop(), result

    def _merged_seats(self, item, families, now, witnessed=()):
        if not isinstance(families, list):
            raise Refused("validation", "families must be a list")
        seats = []
        for value in families:
            family = auth.norm_family(value)
            if family is None:
                raise Refused("family_unknown", "seat_family_empty")
            # Resolve model aliases too: Claude/Anthropic may not masquerade as
            # independent families through different provider/model spellings.
            family = _known_family(family)
            if family is None:
                raise Refused("family_unknown", "seat_family_unknown")
            if family in seats:
                raise Refused("same_family", "duplicate_seat_family")
            seats.append(family)
        need = 2 if item["review_tier"] == "high" else 1
        if len(seats) < need:
            raise Refused("coverage", f"insufficient_seat_families: need {need}")
        if self._card_requires_non_anthropic_seats(item) \
                and len(set(seats) - {"anthropic"}) < 2:
            raise Refused("coverage", "insufficient_non_anthropic_seat_families: need 2")
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
        recorded_seats = {model_family(s["model"]) for s in witnessed}
        for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='run_recorded'",
                (item["key"],)):
            run = json.loads(row["payload"])
            writer = run.get("writer") or {}
            family = _known_family(writer.get("family") or writer.get("model"))
            model = model_family(writer.get("model"))
            if writer and (not family or not model):
                raise Refused("writer_unknown", "recorded run writer family unknown")
            writers.append(family)
            writers.append(model)
            if run.get("phase") == "finished" and run.get("rc") == 0:
                recorded_seats.update(model_family(s.get("model"))
                                      for s in run.get("seats") or ()
                                      if s.get("verdict") == "pass")
        writers = {model_family(w) or auth.norm_family(w) for w in writers}
        if writers.intersection(seats):
            raise Refused("same_family", "writer_seat_family")
        if not set(seats).issubset(recorded_seats - {None}):
            raise Refused("coverage", "seat_family_not_recorded")
        return seats

    def _merged_backfill(self, item, decision_id, head, now, prs=()):
        try:
            decisions = self._ctx(item, now).decisions
            text = " ".join(decisions.text(p["decision_id"]) for p in prs) if prs else decisions.text(decision_id)
            heads = [p["head"] for p in prs] if prs else [head]
            witnessed = self._adapter("spawner_records").review_seats(item, heads, text)
            for bound in heads:
                group = [s for s in witnessed if s["head"] == bound]
                families = list(dict.fromkeys(model_family(s["model"]) for s in group))
                self._merged_seats(item, families, now, group)
            return witnessed
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            raise Refused("evidence_unverified", f"NO-WITNESS {item['key']}: cited review unavailable or unwitnessed") from None

    def _card_requires_non_anthropic_seats(self, item):
        """Shared risk gate for merged closes and high-tier verdicts."""
        card = self.conn.execute("SELECT risk_tags FROM cards WHERE key=?",
                                 (item["key"],)).fetchone()
        tags = []
        if card is not None:
            try:
                tags = json.loads(card["risk_tags"])
            except (ValueError, TypeError, RecursionError):
                raise Refused("validation", "card_risk_tags_invalid") from None
            # Match put_card's canonical vocabulary, not a permissive JSON
            # iterable. Noncanonical spelling is invalid, never lower risk.
            if not isinstance(tags, list) or any(
                    not isinstance(tag, str) or tag not in RISK_TAGS for tag in tags):
                raise Refused("validation", "card_risk_tags_invalid")
        return bool({"auth", "secrets"}.intersection(tags))

    def _close_merged(self, principal, key, decision_id, families, now, from_open=False, apply=False):
        self._require_role(principal, "router")
        if type(from_open) is not bool or type(apply) is not bool:
            raise Refused("validation", "from_open and apply must be booleans")
        if not isinstance(key, str) or not key.strip():
            raise Refused("validation", "key must be a nonblank string")
        item = self._get_or_refuse(key)
        self._merged_review_state(item, from_open)  # DONE is bad_state, not locked
        self._assert_unlocked(item)
        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "decision_id must look like D-NNN")
        head, _ = self._merged_go_proof(item, decision_id, now, all_work=from_open)
        prs = self._merged_open_prs(item, decision_id, head, now) if from_open else []
        backfill = families is None
        witnessed = self._merged_backfill(item, decision_id, head, now, prs) if backfill else []
        families = list(dict.fromkeys(model_family(s["model"]) for s in witnessed)) if backfill else families
        seats = self._merged_seats(item, families, now, witnessed)
        if from_open and not apply:
            return {"key": key, "state": item["state"], "would_close": True, "prs": prs}
        with self._write():
            fresh = self._get_or_refuse(key)
            self._merged_review_state(fresh, from_open)
            self._assert_unlocked(fresh)
            # Lease maintenance changes no close authority. All other fields
            # (including epoch/round, definition and writer proofs) must agree.
            stable = lambda row: {k: v for k, v in row.items()
                                  if k not in ("updated_at", "lease_until")}
            if stable(fresh) != stable(item):
                raise Refused("conflict", f"{key} changed under the transaction")
            current_head, result = self._merged_go_proof(fresh, decision_id, now, all_work=from_open)
            if current_head != head:
                raise Refused("conflict", "merged_on_default_changed")
            if from_open and self._merged_open_prs(fresh, decision_id, head, now) != prs:
                raise Refused("conflict", "merged_pr_proofs_changed")
            if backfill:
                current = self._merged_backfill(fresh, decision_id, head, now, prs)
                if current != witnessed:
                    raise Refused("conflict", "review_witness_changed")
            seats = self._merged_seats(fresh, seats, now, witnessed)
            if witnessed:
                self._record_merged_seats(principal, fresh, decision_id, witnessed, now)
                for family in seats:
                    self._append(principal, key, "review_verdict", {
                        "reviewer": principal.name, "family": family, "ok": True,
                        "epoch": fresh["epoch"], "round": fresh["round"],
                        "source": "cited-run-backfill"}, now)
            ref = {"id": decision_id, "families": seats,
                   "merged_head": head, "caller": principal.name}
            if from_open:
                ref["prs"] = prs
                self.conn.execute("UPDATE items SET state='in_review', lease_until=NULL WHERE key=?", (key,))
                self._append(principal, key, "submitted", {"round": fresh["round"]}, now)
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
        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "decision_id must look like D-NNN")
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
