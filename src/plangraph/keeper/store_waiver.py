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
from .evidence import deploy_hosts, deploy_missing, deploy_targets
from .evidence_types import Result, as_epoch, valid_commit_ref
from .schema import RISK_TAGS
from ..review.family import FAMILIES, model_family


def _known_family(value):
    family = auth.norm_family(value)
    return family if family in {f for _, f in FAMILIES} else model_family(family)


# --- reading the GO verdict of a decision row (pure text, no I/O) ---
# A row is a GO when its decision cell starts with ``GO`` (after the router's timestamp
# prefix) or is a ``RULINGS`` row with exactly one numbered ruling that starts with ``GO``.
# The statement may name a pull request (``GO #12``, ``GO <project> PR #12``, ``GO <project>
# PR 12``) and one full ``GO-HEAD`` token; both bind the verdict to one merged PR head.
_GO_PREFIX = re.compile(r"^\(~?(?:[01][0-9]|2[0-3]):[0-5][0-9x]Z(?:, date -u)?\)\s+")
_GO_RULING = re.compile(r"(?:(?<=\s)|^)\((\d{1,2})\)\s+")
_GO_HEAD = re.compile(r"(?<![\w-])GO-HEAD\s+`?([0-9a-fA-F]{40})(?![\w])")


def _strip_prefix(text):
    return _GO_PREFIX.sub("", text) if isinstance(text, str) else text


def _go_statement(text):
    """The text of the GO verdict, or ``None`` when the row holds no single GO."""
    text = _strip_prefix(text)
    if not isinstance(text, str):
        return None
    if re.match(r"^GO(?=$|\s)", text):
        return text
    if not re.match(r"^RULINGS\b", text):
        return None
    marks = list(_GO_RULING.finditer(text))
    goes = [i for i, m in enumerate(marks)
            if re.match(r"GO(?=$|\s)", text[m.end():])]
    if len(goes) != 1:
        return None
    start = marks[goes[0]].end()
    end = marks[goes[0] + 1].start() if goes[0] + 1 < len(marks) else len(text)
    return text[start:end].strip()


def _go_binding(statement, project):
    """``(pr, head)`` of a GO that names one PR and one full GO-HEAD, else ``(None, None)``."""
    if not isinstance(statement, str):
        return None, None
    end = r"(?=$|[\s,;:.])"
    named = (re.match(rf"^GO\s+(?:{re.escape(project)}\s+PR\s+(?:#|`#)?|(?:`#|#))([0-9]+)`?{end}", statement))
    if named and statement[:named.end()].count("`") % 2:
        named = None
    heads = _GO_HEAD.findall(statement)
    if not named or len(set(h.lower() for h in heads)) != 1:
        return None, None
    return int(named[1]), heads[0].lower()


def _names_item(statement, key):
    """True when the statement names the item by full key or bare id (whole token)."""
    bare = key.split(":", 1)[-1]
    return any(re.search(rf"(?<![\w:-]){re.escape(name)}(?![\w:-])", statement)
               for name in (key, bare))


class WaiverMixin:
    """Router decision verbs for reviews: waive a family or close merged work."""

    def close_merged(self, principal, key, decision_id, families=None, *, from_open=False, apply=False, waive_seats=None):
        audit_key = key if isinstance(key, str) else None
        return self._run(principal, "close_merged", audit_key,
                         lambda now: self._close_merged(principal, key, decision_id,
                                                       families, now, from_open, apply, waive_seats))

    @staticmethod
    def _merged_review_state(item, from_open=False):
        if item["state"] not in (("open", "claimed") if from_open else ("in_review",)):
            raise Refused("bad_state", f"{item['key']} is {item['state']}, not in_review")
        if item.get("review_tier") not in ("standard", "high"):
            raise Refused("bad_state", "close_merged requires review_tier=standard or high")

    def _merge_project(self, item):
        """The project whose registered repository holds the item's merge.

        An item names another repository with a ``Merge-Repo: <project>`` line in
        its description or note; the name must be registered with the git adapter.
        """
        text = f"{item.get('description') or ''}\n{item.get('note') or ''}"
        hint = re.findall(r"(?m)^[ \t]*Merge-Repo:[ \t]*([A-Za-z0-9_.-]+)[ \t]*$", text)
        if not hint:
            return item["project"]
        if len(set(hint)) != 1 or not self._adapter("git").repo_path(hint[0]):
            raise Refused("evidence_unverified", f"merge_repo_unknown: {hint[0]}")
        return hint[0]

    def _merged_heads(self, item, all_work=False):
        """Heads of the merges that introduced the item's Plan-Item commits."""
        git, project = self._adapter("git"), self._merge_project(item)
        try:
            commits = context_sources.plan_item_commits(
                git, project, item["key"], fetch=False,
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
                repo, ref = git.repo_path(project), git.repo_ref(project)
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
        return heads

    def _deploy_pending(self, item):
        """``host:unit`` pairs a merged item ships but has no verified ``deploy`` proof for.

        An item whose merged commits touch ``deploy/systemd`` or add a host binary
        is not done until every host it names reports the unit active.
        """
        git, project = self._adapter("git"), self._merge_project(item)
        try:
            commits = context_sources.plan_item_commits(
                git, project, item["key"], fetch=False, limit=None)
            repo, ref = git.repo_path(project), git.repo_ref(project)
            # The landed changes of each Plan-Item commit's PR, not only the tagged commits.
            changes = {c for commit in commits or () if valid_commit_ref(commit.get("sha"))
                       for c in git.deploy_changes(repo, commit["sha"], ref)}
        except Exception:  # noqa: BLE001 - an unreadable diff never blocks the close
            return []
        targets = deploy_targets(changes)
        if not targets:
            return []
        return deploy_missing(targets, deploy_hosts(item), self._evidence(item["key"]))

    def _deploy_pending_result(self, item, pending, now):
        """Leave the item open: record the reason as an alert and report it."""
        msg = "deploy_pending: " + ", ".join(pending)
        with self._write():
            if self._has_finding(item["key"], "deploy_pending"):
                self._alerts_raise_finding(item["key"], "deploy_pending", msg, now)
            else:
                self._finding(item["key"], "deploy_pending", msg, now)
        return {"key": item["key"], "state": item["state"], "closed": False,
                "reason": "deploy_pending", "missing": pending}

    def _merged_go_proof(self, item, decision_id, now, *, all_work=False):
        heads = self._merged_heads(item, all_work)
        looked = ", ".join(sorted(h[:12] for h in heads)) or "none"
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
        text = _strip_prefix(text)
        statement = _go_statement(text)
        if statement is None:
            raise Refused("evidence_unverified",
                          f"go_row_not_go: no GO verdict in {decision_id} (merged heads {looked})")
        text = statement
        pr = r"(?:#[0-9]+|`#[0-9]+`)(?=$|[\s,;:.])"
        if (re.match(rf"^GO\s+{pr}", text)
                or re.match(rf"^GO\s+{re.escape(item['project'])}\s+PR\s+{pr}", text)
                or _go_binding(text, item["project"])[0]):
            # Review prose may itself discuss a NO GO; authority is the leading
            # GO verdict and its one explicit, full-length GO-HEAD token.
            bounds = re.findall(r"(?<![\w-])GO-HEAD\s+`?([0-9a-fA-F]{40})(?![\w])", text)
        else:
            if re.search(r"(?i)\b(?:no|not)\b.*\bgo\b", text):
                raise Refused("evidence_unverified",
                              f"go_row_not_go: {decision_id} discusses a NO GO (merged heads {looked})")
            bound = re.match(r"^GO\s+`?([0-9a-fA-F]{8,64})(?![\w])", text)
            bounds = [bound[1]] if bound else []
        matched = {head for head in heads if len(bounds) == 1
                   and head.startswith(bounds[0].lower())}
        if len(matched) != 1:
            matched = self._merged_pr_bound_head(item, text, now)
        if len(matched) != 1:
            raise Refused("evidence_unverified",
                          f"go_row_head_mismatch: {decision_id} names "
                          f"{', '.join(b[:12] for b in bounds) or 'no head'}, merged heads {looked}")
        return matched.pop(), result

    def _merged_pr_bound_head(self, item, text, now):
        """The GO-HEAD of a row that names a merged PR and this item, as a one-head set.

        The PR the row names must have merged on the default branch with exactly
        that head, and the row must name the item; the trailer search is not asked.
        """
        pr, head = _go_binding(text, item["project"])
        if pr is None or not _names_item(text, item["key"]):
            return set()
        try:
            merged = context_sources.pr_merge_of(
                self._adapter("git"), self._merge_project(item), head,
                fallback_pr=pr)
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - an unreadable merge never authorizes
            return set()
        return {head} if merged and merged["pr"] == pr else set()

    def _merged_waiver_only(self, item, waiver_id, now, *, all_work=False):
        """Close proof on the operator waiver row alone (no GO row, no PR number asked).

        The row must be an OPERATOR ``Waiver: merged-without-witnesses`` row that lists
        the key; every Plan-Item commit of the item is on the default branch and merged
        before the row's cutoff, and no open lane remains.  Returns
        ``(head, proofs, waiver, verification)``.
        """
        git, project = self._adapter("git"), self._merge_project(item)
        if not self._refresh_merged_default_ref(git, project):
            raise Refused("evidence_unverified", "merged_default_ref_refresh_failed")
        if context_sources.open_lanes(git, project, item["key"]) != []:
            raise Refused("evidence_unverified", "unmerged_work_or_lanes_unknown")
        commits = context_sources.plan_item_commits(
            git, project, item["key"], fetch=False, limit=None)
        if not commits:
            raise Refused("evidence_unverified", "merged_on_default_missing")
        waiver = self._merged_seat_waiver(item, waiver_id, waiver_id, [], now)
        repo, proofs = git.repo_path(project), []
        for commit in commits:
            merge = commit.get("merge") or {}
            if merge.get("sha"):
                landed = valid_commit_ref(merge["sha"])
                proc = git._run(repo, ["show", "-s", "--format=%P%x20%cI",
                                       "--end-of-options", merge["sha"]]) if landed else None
                fields = proc.stdout.split() if proc and proc.returncode == 0 else []
                if len(fields) != 3 or not all(valid_commit_ref(p) for p in fields[:2]):
                    raise Refused("evidence_unverified", "merged_pr_head_missing")
                head, date = fields[1], fields[2]
            else:  # a direct commit on the default branch
                head, date = commit.get("sha"), commit.get("date")
            merged_at = as_epoch(date)
            if not valid_commit_ref(head) or merged_at is None:
                raise Refused("evidence_unverified", "operator_seat_waiver_merge_date_missing")
            if merged_at >= waiver["cutoff"]:
                raise Refused("evidence_unverified", "operator_seat_waiver_merge_at_or_after_cutoff")
            pr = merge.get("pr") if type(merge.get("pr")) is int else None
            if all(p["head"] != head for p in proofs):
                proofs.append({"pr": pr, "head": head, "decision_id": waiver_id})
        result = self._verify_evidence(item, "decision", {"id": waiver_id}, now)
        if not result.ok:
            raise Refused("evidence_unverified", result.detail)
        return proofs[0]["head"], proofs, waiver, result

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
        writers = self._merged_writer_families(item, now)
        recorded_seats = {model_family(s["model"]) for s in witnessed}
        for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='run_recorded'",
                (item["key"],)):
            run = json.loads(row["payload"])
            if run.get("phase") == "finished" and run.get("rc") == 0:
                recorded_seats.update(model_family(s.get("model"))
                                      for s in run.get("seats") or ()
                                      if s.get("verdict") == "pass")
        if writers.intersection(seats):
            raise Refused("same_family", "writer_seat_family")
        if not set(seats).issubset(recorded_seats - {None}):
            raise Refused("coverage", "seat_family_not_recorded")
        return seats

    def _merged_writer_families(self, item, now):
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
            run = json.loads(row["payload"])
            writer = run.get("writer") or {}
            family = _known_family(writer.get("family") or writer.get("model"))
            model = model_family(writer.get("model"))
            if writer and (not family or not model):
                raise Refused("writer_unknown", "recorded run writer family unknown")
            writers.append(family)
            writers.append(model)
        return {model_family(w) or auth.norm_family(w) for w in writers if w}

    def _merged_backfill(self, item, decision_id, head, now, prs=()):
        try:
            decisions = self._ctx(item, now).decisions
            text = " ".join(decisions.text(p["decision_id"]) for p in prs) if prs else decisions.text(decision_id)
            heads = [p["head"] for p in prs] if prs else [head]
            claude = [e for e in self._evidence(item["key"])
                      if e["epoch"] == item["epoch"] and e["kind"] == "claude-seat"]
            if (claude and item.get("kind") == "lane"
                    and re.search(r"(?<![\w-])D-4504(?![\w-])",
                                  item.get("done_criteria", "") + " " + text)):
                rows = [decisions.text(p["decision_id"]) for p in prs] if prs else [text]
                if not all(re.search(r"(?:^|;)\s*Witness: claude-seat\s*(?=;|$)", row) for row in rows):
                    raise Refused("evidence_unverified", "claude_seat_lane_witness_rule")
            witnessed = []
            for evidence in claude:
                ref = evidence["ref"]
                if ref.get("head") not in heads:
                    raise Refused("evidence_unverified", "claude_seat_head")
                result = self._verify_evidence(item, "claude-seat", ref, now)
                if not result.ok:
                    raise Refused("evidence_unverified", result.detail)
                witnessed.append(result.ref["seat"])
            # Existing spawner citations still fail closed; absence is allowed
            # only when transcript evidence supplies the review witnesses.
            if not claude or json.loads(item.get("run_ids") or "[]") or re.search(r"\brun-", text):
                witnessed.extend(self._adapter("spawner_records").review_seats(
                    item, heads, text, allow_cli_reported=not self._card_requires_non_anthropic_seats(item),
                    **({"allow_empty": True} if claude else {})))
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

    def _close_merged(self, principal, key, decision_id, families, now, from_open=False, apply=False, waive_seats=None):
        self._require_role(principal, "router")
        if type(from_open) is not bool or type(apply) is not bool:
            raise Refused("validation", "from_open and apply must be booleans")
        if not isinstance(key, str) or not key.strip():
            raise Refused("validation", "key must be a nonblank string")
        if families is not None and waive_seats is not None:
            raise Refused("validation", "families_with_waive_seats")
        if families is not None and not isinstance(families, list):
            raise Refused("validation", "families must be a list")
        item = self._get_or_refuse(key)
        self._merged_review_state(item, from_open)  # DONE is bad_state, not locked
        self._assert_unlocked(item)
        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "decision_id must look like D-NNN")
        # The operator waiver row itself as the decision: no GO row is read.
        waiver_only = waive_seats is not None and waive_seats == decision_id
        with self._decisions_snapshot(item, now):
            if waiver_only:
                head, prs, waiver, _ = self._merged_waiver_only(item, decision_id, now, all_work=from_open)
            else:
                head, _ = self._merged_go_proof(item, decision_id, now, all_work=from_open)
                prs = self._merged_open_prs(item, decision_id, head, now) if from_open or waive_seats is not None else []
                waiver = self._merged_seat_waiver(item, decision_id, waive_seats, prs, now) if waive_seats is not None else None
            backfill = families is None and waiver is None
            witnessed = self._merged_backfill(item, decision_id, head, now, prs) if backfill else []
            families = list(dict.fromkeys(model_family(s["model"]) for s in witnessed)) if backfill else families
            seats = self._merged_seats(item, families, now, witnessed) if waiver is None else []
            pending = self._deploy_pending(item)
            if pending:
                return self._deploy_pending_result(item, pending, now)
            if (from_open or waiver_only) and not apply:
                return {"key": key, "state": item["state"], "would_close": True, "prs": prs}
        with self._write(), self._decisions_snapshot(item, now):  # a fresh read under the lock
            fresh = self._get_or_refuse(key)
            self._merged_review_state(fresh, from_open)
            self._assert_unlocked(fresh)
            # Lease maintenance changes no close authority. All other fields
            # (including epoch/round, definition and writer proofs) must agree.
            stable = lambda row: {k: v for k, v in row.items()
                                  if k not in ("updated_at", "lease_until")}
            if stable(fresh) != stable(item):
                raise Refused("conflict", f"{key} changed under the transaction")
            if waiver_only:
                current_head, current_prs, current_waiver, result = self._merged_waiver_only(
                    fresh, decision_id, now, all_work=from_open)
                if current_head != head:
                    raise Refused("conflict", "merged_on_default_changed")
                if current_prs != prs:
                    raise Refused("conflict", "merged_pr_proofs_changed")
                if current_waiver != waiver:
                    raise Refused("conflict", "merged_seat_waiver_changed")
            else:
                current_head, result = self._merged_go_proof(fresh, decision_id, now, all_work=from_open)
                if current_head != head:
                    raise Refused("conflict", "merged_on_default_changed")
                if (from_open or waiver) and self._merged_open_prs(fresh, decision_id, head, now) != prs:
                    raise Refused("conflict", "merged_pr_proofs_changed")
                if waiver and self._merged_seat_waiver(fresh, decision_id, waive_seats, prs, now) != waiver:
                    raise Refused("conflict", "merged_seat_waiver_changed")
            if backfill:
                current = self._merged_backfill(fresh, decision_id, head, now, prs)
                if current != witnessed:
                    raise Refused("conflict", "review_witness_changed")
            seats = self._merged_seats(fresh, seats, now, witnessed) if waiver is None else []
            if witnessed:
                self._record_merged_seats(principal, fresh, decision_id, witnessed, now)
                for family in seats:
                    self._append(principal, key, "review_verdict", {
                        "reviewer": principal.name, "family": family, "ok": True,
                        "epoch": fresh["epoch"], "round": fresh["round"],
                        "source": "cited-run-backfill"}, now)
            ref = {"id": decision_id, "families": seats,
                   "merged_head": head, "caller": principal.name}
            if waiver:
                self._card_requires_non_anthropic_seats(fresh)  # validate the recorded risk tags
                card = self.conn.execute("SELECT risk_tags FROM cards WHERE key=?", (key,)).fetchone()
                ref.update(waiver={k: v for k, v in waiver.items() if k not in ("scope", "cutoff")}, prs=prs, review_tier=fresh["review_tier"],
                           risk_tags=json.loads(card["risk_tags"]) if card else [],
                           seat_rule_waived_by=waive_seats)
            if from_open:
                ref["prs"] = prs
                self.conn.execute("UPDATE items SET state='in_review', lease_until=NULL WHERE key=?", (key,))
                # The item goes straight on to done: a run that still owns it is retired here.
                self._exec_clear(self.conn, key, principal, now)
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
