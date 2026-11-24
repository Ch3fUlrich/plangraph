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

from . import auth, context_sources, timing
from .coverage import writer_families
from .errors import Refused
from .evidence_types import Result, as_epoch, has_live_clause, live_evidence_missing, valid_commit_ref
from .store_runs import go_key_scope
from .schema import RISK_TAGS
from ..review.family import FAMILIES, model_family


def _known_family(value):
    family = auth.norm_family(value)
    return family if family in {f for _, f in FAMILIES} else model_family(family)


# A row is a GO when its decision cell starts with ``GO`` (after the router's timestamp
# prefix) or is a ``RULINGS`` row with exactly one numbered ruling that starts with ``GO``.
# The statement may name a pull request (``GO #12``, ``GO <project> PR #12``, ``GO <project>
# PR 12``) and one full ``GO-HEAD`` token; both bind the verdict to one merged PR head.
GO_PREFIX = re.compile(r"^\(~?(?:[01][0-9]|2[0-3]):[0-5][0-9x]Z(?:, date -u)?\)\s+")
_GO_RULING = re.compile(r"(?:(?<=\s)|^)\((\d{1,2})\)\s+")
_GO_HEAD = re.compile(r"(?<![\w-])GO-HEAD\s+`?([0-9a-fA-F]{40})(?![\w])")


def _strip_prefix(text):
    return GO_PREFIX.sub("", text) if isinstance(text, str) else text


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
    if not named or len(heads) != 1:
        return None, None
    return int(named[1]), heads[0].lower()


def _go_leading_head(statement):
    """The 8 to 64 hex digits a GO statement leads with (``GO <head>``), else ``None``."""
    bound = re.match(r"^GO\s+`?([0-9a-fA-F]{8,64})(?![\w])", statement)
    return bound[1] if bound else None


def _names_item(statement, key):
    """True when the statement names the item by full key or bare id (whole token)."""
    if (scope := go_key_scope(statement)) is not None:
        return key in scope
    bare = key.split(":", 1)[-1]
    return any(re.search(rf"(?<![\w:-]){re.escape(name)}(?![\w:-])", statement)
               for name in (key, bare))


class WaiverMixin:
    """Router decision verbs for reviews: waive a family or close merged work."""

    def close_merged(self, principal, key, decision_id, families=None, *, from_open=False, apply=False,
                     waive_seats=None, dry_run=False, require_seats=None):
        audit_key = key if isinstance(key, str) else None
        return self._run(principal, "close_merged", audit_key,
                         lambda now: self._close_merged(principal, key, decision_id,
                                                       families, now, from_open, apply, waive_seats, dry_run, require_seats),
                         success_message=None if dry_run is True else "merged_on_default")

    @staticmethod
    def _merged_review_state(item, from_open=False):
        if item["state"] not in (("open", "claimed") if from_open else ("in_review",)):
            raise Refused("bad_state", f"{item['key']} is {item['state']}, not in_review")
        if item.get("review_tier") not in ("standard", "high"):
            raise Refused("bad_state", "close_merged requires review_tier=standard or high")

    @staticmethod
    def _only_merge_repo_note_changed(old, new):
        if not isinstance(new, str):
            return False
        pattern = context_sources.MERGE_REPO_LINE
        old_lines, new_lines = (old or "").splitlines(), new.splitlines()
        hints = [pattern.fullmatch(line) for line in new_lines]
        return (sum(bool(hint) for hint in hints) == 1
                and [line for line, hint in zip(new_lines, hints) if not hint]
                == [line for line in old_lines if not pattern.fullmatch(line)])

    def _merge_repository(self, item):
        """The project whose registered repository holds the item's merge.

        An item names another repository with a ``Merge-Repo: <project>`` line in
        its description or note. A note can also name the merge SHA or #PR.
        """
        text = f"{item.get('description') or ''}\n{item.get('note') or ''}"
        hints = [context_sources.MERGE_REPO_LINE.fullmatch(line) for line in text.splitlines()
                 if line.lstrip().startswith("Merge-Repo:")]
        if not hints:
            return item.get("_merge_project") or item["project"], None
        if any(hint is None for hint in hints):
            raise Refused("evidence_unverified", "merge_repo_note_invalid")
        projects = {hint[1] for hint in hints}
        revisions = {hint[2] for hint in hints if hint[2]}
        project = hints[0][1]
        if len(projects) != 1 or len(revisions) > 1:
            raise Refused("evidence_unverified", "merge_repo_note_conflict")
        if not self._adapter("git").repo_path(project):
            raise Refused("evidence_unverified", f"merge_repo_unknown: {project}")
        return project, next(iter(revisions), None)

    def _scan_projects(self, item):
        """Projects to read for the item's merge: its own first, then every scanned repository."""
        scan = getattr(self._adapter("git"), "scan_projects", None)
        names = list(scan()) if callable(scan) else []
        return [*([item["project"]] if item["project"] in names else []), *[n for n in names if n != item["project"]]]

    def _merge_project(self, item):
        return self._merge_repository(item)[0]

    def _named_merge_proof(self, item):
        project, revision = self._merge_repository(item)
        if revision is None:
            return None
        git = self._adapter("git")
        head = item.get("_close_head")
        if head is None and not self._refresh_merged_default_ref(git, project):
            raise Refused("evidence_unverified", f"merge_repo_default_refresh_failed: {project}")
        proof = context_sources.named_merge(git, project, revision, head=head)
        if proof is None:
            raise Refused("evidence_unverified", f"merge_repo_commit_not_on_default: {project} {revision}")
        return proof

    def _merged_go_proof(self, item, decision_id, now, *, all_work=False):
        project = self._merge_project(item)
        cross_repo = project != item["project"]
        if cross_repo and item.get("_merge_project"):
            self._require_inferred_repo_scope(item, decision_id, now)
        named = self._named_merge_proof(item)
        heads = self._merged_heads(item, all_work)
        if named:
            heads.add(named["head"])
        looked = ", ".join(sorted(h[:12] for h in heads)) or "none"
        if not heads and not cross_repo:
            raise Refused("evidence_unverified", "merged_on_default_missing")
        result = self._verify_evidence(item, "decision", {"id": decision_id}, now)
        if not result.ok:
            raise Refused("evidence_unverified", result.detail)
        try:
            text = self._ctx(item, now).decisions.text(decision_id)
        except Exception:  # noqa: BLE001 - absent/unreadable row text never authorizes
            text = None
        # Only the router's timestamp prefix may precede the GO verdict.
        text = _strip_prefix(text)
        statement = _go_statement(text)
        if statement is None:
            raise Refused("evidence_unverified",
                          f"go_row_not_go: no GO verdict in {decision_id} (merged heads {looked})")
        text = statement
        if (scope := go_key_scope(text)) is not None and item["key"] not in scope:
            raise Refused("evidence_unverified", f"merged_pr_go_scope_mismatch: {item['key']} in {decision_id}")
        pr = r"(?:#[0-9]+|`#[0-9]+`)(?=$|[\s,;:.])"
        projects = (item["project"], project)
        bindings = [_go_binding(text, candidate)[0] for candidate in projects]
        if (re.match(rf"^GO\s+{pr}", text) or any(bindings)
                or any(re.match(rf"^GO\s+{re.escape(candidate)}\s+PR\s+{pr}", text)
                       for candidate in projects)):
            # Review prose may itself discuss a NO GO; authority is the leading
            # GO verdict and its one explicit, full-length GO-HEAD token.
            bounds = re.findall(r"(?<![\w-])GO-HEAD\s+`?([0-9a-fA-F]{40})(?![\w])", text)
        else:
            if re.search(r"(?i)\b(?:no|not)\b.*\bgo\b", text):
                raise Refused("evidence_unverified",
                              f"go_row_not_go: {decision_id} discusses a NO GO (merged heads {looked})")
            bound = _go_leading_head(text)
            bounds = [bound] if bound else []
        matched = {head for head in heads if len(bounds) == 1
                   and head.startswith(bounds[0].lower())}
        if len(matched) != 1:
            if not cross_repo or self._merged_go_scope_matches(decision_id, item, now):
                matched = self._merged_pr_bound_head(item, text)
        if not heads and not matched:
            raise Refused("evidence_unverified", "merged_on_default_missing")
        if named and (matched != {named["head"]} or not _names_item(text, item["key"])):
            raise Refused("evidence_unverified", "merge_repo_go_scope_or_head_mismatch")
        if named and named["pr"] and any(bindings) and named["pr"] not in bindings:
            raise Refused("evidence_unverified", "merge_repo_go_pr_mismatch")
        if len(matched) != 1:
            raise Refused("evidence_unverified",
                          f"go_row_head_mismatch: {decision_id} names "
                          f"{', '.join(b[:12] for b in bounds) or 'no head'}, merged heads {looked}")
        return matched.pop(), result

    def _merged_pr_bound_head(self, item, text):
        """The GO-HEAD of a row that names a merged PR and this item, as a one-head set.

        The PR the row names must have merged on the default branch with exactly
        that head, and the row must name the item; the trailer search is not asked.
        """
        project = self._merge_project(item)
        pr, head = _go_binding(text, project)
        if pr is None and project != item["project"]:
            pr, head = _go_binding(text, item["project"])
        if pr is None or not _names_item(text, item["key"]):
            return set()
        try:
            merged = context_sources.pr_merge_of(
                self._adapter("git"), self._merge_project(item), head,
                fallback_pr=pr, default_head=item.get("_close_head"))
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - an unreadable merge never authorizes
            return set()
        return {head} if merged and merged["pr"] == pr else set()

    def _merged_work_commits(self, item):
        """Every Plan-Item commit of the item on the default ref; refuses while a lane is open or unknown."""
        git, project = self._adapter("git"), self._merge_project(item)
        warm = self._warm(project)
        if self.__dict__.get("_frozen_lanes") and not warm:   # the proof never refreshes a frozen scan
            raise Refused("evidence_unverified", f"merge_index_not_warm: {project}")
        head = item.get("_close_head")
        if not warm and head is None and not self._refresh_merged_default_ref(git, project):
            raise Refused("evidence_unverified", "merged_default_ref_refresh_failed")
        # The automatic run never asks the forge under the lock: an expired snapshot answers nothing (unknown).
        kw = ({"cache": self.__dict__.setdefault("_lanes", {}) if warm else {}, "frozen": True}
              if warm or self.__dict__.get("_frozen_lanes") else {})
        if head is not None:
            kw["head"] = head
        if context_sources.open_lanes(git, project, item["key"], **kw) != []:
            raise Refused("evidence_unverified", "unmerged_work_or_lanes_unknown")
        index = self.__dict__.get("_warmed", {}).get(project)
        if self.__dict__.get("_frozen_lanes") and index and head == index.get("target"):
            commits = context_sources.indexed_work(index, item["key"], limit=None)["commits"]
        else:
            commits = context_sources.plan_item_commits(
                git, project, item["key"], fetch=False, limit=None, head=head)
        if not commits:
            raise Refused("evidence_unverified", "merged_on_default_missing")
        return commits

    @staticmethod
    def _merged_commit_proof(git, project, commit):
        """``(pr, head, date)`` of one Plan-Item commit on the default ref.

        A merged PR binds to its second parent and the merge date; a direct
        commit on the default branch binds to itself.
        """
        merge = commit.get("merge") or {}
        if merge.get("sha"):
            repo = git.repo_path(project)
            landed = valid_commit_ref(merge["sha"])
            try:
                if landed and hasattr(git, "merge_proof_fields"):
                    fields = git.merge_proof_fields(repo, merge["sha"])
                else:
                    proc = git._run(repo, ["show", "-s", "--format=%P%x20%cI",
                                           "--end-of-options", merge["sha"]]) if landed else None
                    fields = proc.stdout.split() if proc and proc.returncode == 0 else []
            except (OSError, RuntimeError, ValueError):
                fields = []
            if len(fields) != 3 or not all(valid_commit_ref(p) for p in fields[:2]):
                raise Refused("evidence_unverified", "merged_pr_head_missing")
            head, date = fields[1], fields[2]
        else:  # a direct commit on the default branch
            head, date = commit.get("sha"), commit.get("date")
        if not valid_commit_ref(head):
            raise Refused("evidence_unverified", "operator_seat_waiver_merge_date_missing")
        return (merge.get("pr") if type(merge.get("pr")) is int else None), head, date

    def _merged_waiver_only(self, item, waiver_id, now, *, all_work=False):
        """Close proof on the operator waiver row alone (no GO row, no PR number asked).

        The row must be an OPERATOR ``Waiver: merged-without-witnesses`` row that lists
        the key; every Plan-Item commit of the item is on the default branch and merged
        before the row's cutoff, and no open lane remains.  Returns
        ``(head, proofs, waiver, verification)``.
        """
        git, project = self._adapter("git"), self._merge_project(item)
        commits = self._merged_work_commits(item)
        waiver = self._merged_seat_waiver(item, waiver_id, waiver_id, [], now)
        proofs = []
        for commit in commits:
            pr, head, date = self._merged_commit_proof(git, project, commit)
            merged_at = as_epoch(date)
            if merged_at is None:
                raise Refused("evidence_unverified", "operator_seat_waiver_merge_date_missing")
            if merged_at >= waiver["cutoff"]:
                raise Refused("evidence_unverified", "operator_seat_waiver_merge_at_or_after_cutoff")
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

    def _landed_writer_families(self, item):
        """Families named by the ``Writer`` trailers of the commits that landed the item's Plan-Item work."""
        git, project = self._adapter("git"), self._merge_project(item)
        families = []
        try:
            head = item.get("_close_head")
            repo, ref = git.repo_path(project), head or git.repo_ref(project)
            for commit in context_sources.plan_item_commits(
                    git, project, item["key"], fetch=False, limit=None, head=head) or ():
                landed = [commit["sha"], (commit.get("merge") or {}).get("sha")]
                # the commit's own range, its own trailers and the trailers of the merge that landed it
                trailers = [*git.writer_trailers(repo, commit["sha"], ref),
                            *(git.trailers(repo, sha) for sha in landed if sha)]
                for trailer in trailers:
                    model = str(trailer.get("Writer") or "").split()[:1]
                    families.append(model_family(model[0]) if model else None)
        except Exception:  # noqa: BLE001 - an unreadable commit names no writer; the recorded ones still count
            pass
        return families

    def _merged_writer_families(self, item, now):
        writers = writer_families(self, item, self._ctx(item, now))
        writers.append(item.get("owner_family"))
        if item.get("writer_model"):
            family = model_family(item["writer_model"])
            if family is None:
                raise Refused("writer_unknown", "recorded writer model family unknown")
            writers.append(family)
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
        writers.extend(self._landed_writer_families(item))
        if not any(writers):
            # No writer is on record: the claimant and delegate stand for it (fail closed).
            # With a recorded writer they are the orchestrating principals, not the diff's authors.
            for name in (item.get("claimed_by"), item.get("delegate")):
                peer = auth.get_principal(self.conn, name)
                if peer is not None:
                    writers.append(peer.family)
        return {model_family(w) or auth.norm_family(w) for w in writers if w}

    def _merged_backfill(self, item, decision_id, head, now, prs=()):
        try:
            # No decision row (the automatic merge close): the merge is the proof,
            # so the witnesses come from the item's own runs and seat evidence.
            decisions = self._ctx(item, now).decisions if decision_id is not None else None
            rows = ([""] if decision_id is None
                    else [decisions.text(p["decision_id"]) for p in prs] if prs
                    else [decisions.text(decision_id)])
            text = " ".join(rows)
            heads = [p["head"] for p in prs] if prs else [head]
            try:
                parts = self._merged_cited_parts(item, heads)
            except Exception:  # noqa: BLE001 - an unreadable citation never adds a witness
                parts = {}
            if parts:  # a batch landed these parts: their seats bind the part heads, other heads keep theirs
                heads = [part for h in heads for part in parts.get(h, {h: ()})]
                text += " Review-Runs: " + ", ".join(
                    r for by_part in parts.values() for runs in by_part.values() for r in runs)
            claude = [e for e in self._evidence(item["key"])
                      if e["epoch"] == item["epoch"] and e["kind"] == "claude-seat"]
            if claude and not prs:
                # A merge made of several parts: seat evidence for another merged head of the item counts per part.
                merged = self._merged_heads(item, all_work=True)
                heads += [h for h in dict.fromkeys(e["ref"].get("head") for e in claude)
                          if h in merged and h not in heads]
            if (claude and item.get("kind") == "lane"
                    and re.search(r"(?<![\w-])D-4504(?![\w-])",
                                  item.get("done_criteria", "") + " " + text)):
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

    def _close_merged(self, principal, key, decision_id, families, now, from_open=False, apply=False,
                      waive_seats=None, dry_run=False, require_seats=None):
        self._require_role(principal, "router")
        if (from_open is not None and type(from_open) is not bool
                or any(type(value) is not bool for value in (apply, dry_run))):
            raise Refused("validation", "from_open must be a boolean when supplied; apply and dry_run must be booleans")
        required = self._seats_required(require_seats)
        if dry_run is True and key is None and decision_id is None:
            return {"dry_run": True, "route": "close_merged"}
        if not isinstance(key, str) or not key.strip():
            raise Refused("validation", "key must be a nonblank string")
        if families is not None and waive_seats is not None:
            raise Refused("validation", "families_with_waive_seats")
        if families is not None and not isinstance(families, list):
            raise Refused("validation", "families must be a list")
        item = self._get_or_refuse(key)
        if from_open is None:
            from_open = item["state"] in ("open", "claimed")
        self._merged_review_state(item, from_open)  # DONE is bad_state, not locked
        self._assert_unlocked(item)
        item = self._with_merge_repo(item)
        if not isinstance(decision_id, str) or not re.fullmatch(r"D-[0-9]+", decision_id):
            raise Refused("validation", "decision_id must look like D-NNN")
        # The operator waiver row itself as the decision: no GO row is read.
        waiver_only = waive_seats is not None and waive_seats == decision_id
        with self._decisions_snapshot(item, now):
            try:
                statement = _go_statement(self._ctx(item, now).decisions.text(decision_id))
            except Exception:  # the GO proof reports unreadable decision text
                statement = None
            scoped_train = go_key_scope(statement) is not None
            all_work = from_open or scoped_train
            complete_history = all_work or waive_seats is not None
            with timing.outside_request_lock():
                item, token, changes = self._manual_deploy_scan(
                    item, complete_history=complete_history, refresh_alternate=not waiver_only)
            if waiver_only:
                head, prs, waiver, _ = self._merged_waiver_only(item, decision_id, now, all_work=from_open)
            else:
                head, _ = self._merged_go_proof(item, decision_id, now, all_work=all_work)
                prs = (self._merged_open_prs(item, decision_id, head, now, waive_seats is not None)
                       if complete_history else [])
                waiver = self._merged_seat_waiver(item, decision_id, waive_seats, prs, now) if waive_seats is not None else None
            backfill = families is None and waiver is None
            witnessed, seats, flags = ([], [], None) if waiver is not None else self._seats_or_flag(
                item, decision_id, head, now, prs, families, backfill, required)
            project, revision = self._merge_repository(item)
            if token is not None and not waiver_only and project != item["project"] and revision is None:
                changes |= self._merged_go_deploy_changes(item, head)
            pending = self._deploy_pending(item, changes, token)
            if live_evidence_missing(item, self._evidence(key)):
                pending = [*pending, "live_evidence_missing"]
            if token is not None and not self._deploy_scan_current(item, token):
                raise Refused("conflict", "merged_on_default_changed")
            if pending:
                return self._deploy_pending_result(item, pending, now, record=not dry_run)
            if dry_run or ((from_open or waiver_only) and not apply):
                return {"key": key, "state": item["state"], "next_state": "done", "would_close": True, "prs": prs, **self._flags_of(flags)}
        with self._write(), self._decisions_snapshot(item, now):  # a fresh read under the lock
            fresh = self._get_or_refuse(key)
            self._merged_review_state(fresh, from_open)
            self._assert_unlocked(fresh)
            # Lease maintenance changes no close authority. All other fields
            # (including epoch/round, definition and writer proofs) must agree.
            stable = lambda row: {k: v for k, v in row.items()
                                  if k not in ("updated_at", "lease_until", "_merge_project", "_close_head")}
            if stable(fresh) != stable(item):
                raise Refused("conflict", f"{key} changed under the transaction")
            fresh.update({field: item[field] for field in ("_merge_project", "_close_head") if field in item})
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
                current_head, result = self._merged_go_proof(fresh, decision_id, now, all_work=all_work)
                if current_head != head:
                    raise Refused("conflict", "merged_on_default_changed")
                if complete_history and self._merged_open_prs(fresh, decision_id, head, now, bool(waiver)) != prs:
                    raise Refused("conflict", "merged_pr_proofs_changed")
                if waiver and self._merged_seat_waiver(fresh, decision_id, waive_seats, prs, now) != waiver:
                    raise Refused("conflict", "merged_seat_waiver_changed")
            if waiver is None:
                current, seats, current_flags = self._seats_or_flag(
                    fresh, decision_id, head, now, prs, families, backfill, required)
                if (backfill and current != witnessed) or current_flags != flags:
                    raise Refused("conflict", "review_witness_changed")
            ref = {"id": decision_id, "families": seats,
                   "merged_head": head, "caller": principal.name, **self._flags_of(flags)}
            if waiver:
                self._card_requires_non_anthropic_seats(fresh)  # validate the recorded risk tags
                card = self.conn.execute("SELECT risk_tags FROM cards WHERE key=?", (key,)).fetchone()
                ref.update(waiver={k: v for k, v in waiver.items() if k not in ("scope", "cutoff")}, prs=prs, review_tier=fresh["review_tier"],
                           risk_tags=json.loads(card["risk_tags"]) if card else [],
                           seat_rule_waived_by=waive_seats)
            if all_work:
                ref["prs"] = prs
            part = None if waiver_only else self._merged_go_scope(decision_id, fresh, now, require_key=False)[1]
            if part:
                ref["go_part"] = part   # the named part head covering a key the row's list omits
            if token is not None and not self._deploy_scan_current(fresh, token):
                raise Refused("conflict", "merged_on_default_changed")
            return self._land_merged(principal, fresh, head, seats, witnessed, decision_id, from_open,
                                     ("decision", ref, result), f"router GO {decision_id} merged {head}", now,
                                     flags=flags)

    def _land_merged(self, principal, fresh, head, seats, witnessed, label, from_open, evidence, reason, now,
                     release=False, flags=None):
        """Write the merged close under the held write lock: seats, evidence, ``done``.

        ``from_open`` first moves an open or claimed item through ``submitted``;
        ``evidence`` is ``(kind, ref, result)``; ``release`` also clears the claim. Used by the router verb and the
        automatic merge close, so both end an item the same way.
        """
        key = fresh["key"]
        if witnessed:
            self._record_merged_seats(principal, fresh, label, witnessed, now)
            for family in seats:
                self._append(principal, key, "review_verdict", {
                    "reviewer": principal.name, "family": family, "ok": True,
                    "epoch": fresh["epoch"], "round": fresh["round"],
                    "source": "cited-run-backfill"}, now)
        if from_open:
            self.conn.execute("UPDATE items SET state='in_review', lease_until=NULL WHERE key=?", (key,))
            # The item goes straight on to done: a run that still owns it is retired here.
            self._exec_clear(self.conn, key, principal, now)
            self._append(principal, key, "submitted", {"round": fresh["round"]}, now)
        kind, ref, result = evidence
        self._record_evidence(principal, fresh, kind, ref, now, Result(True, result.detail, ref))
        return dict(self._pass(principal, key, reason, fresh, now, reviews_passed=len(seats),
                               release=release, flags=flags), closed=True)

    def _auto_close_one(self, principal, item, require_seats, apply, now):
        key = item["key"]
        tier = item.get("review_tier")
        if tier not in ("standard", "high", "none"):
            raise Refused("bad_state", "merge close requires review_tier=standard, high or none")
        if self._card_requires_non_anthropic_seats(item):
            raise Refused("forbidden", "sensitive_card_needs_router_close: auth/secrets risk tag")
        self._assert_unlocked(item)
        seated = tier != "none"
        token, changes = self._auto_deploy_scan(item)
        pinned = dict(item, _close_head=token[0])
        proofs = self._merged_auto_proofs(pinned)
        pending = self._deploy_pending(pinned, changes, token)
        missing = list(pending)
        if self._live_evidence_missing(item, proofs):
            missing.append("live_evidence_missing")
        result = {}
        if missing:
            landed = [(stamp, proof["merged_at"]) for proof in proofs
                      if (stamp := as_epoch(proof["merged_at"])) is not None]
            earliest = min(landed, default=None)
            result["deployment_pending"] = {
                "missing": missing, "merged_at": earliest[1] if earliest else None,
                "age_hours": int(max(0, now - earliest[0]) // 3600) if earliest else None}
        if missing:
            # A shipped unit without deploy proof, or a Live: clause without live evidence, holds the item.
            if apply:
                self._deploy_pending_result(item, missing, now)
            return result
        witnessed, flags = self._auto_seats(pinned, proofs, now, require_seats) if seated else ([], None)
        landing = self._landing_evidence(pinned, proofs, now)
        gaps = {"landing_flags": landing["flags"]} if landing["flags"] else {}
        if not apply:
            return {**result, "key": key, "state": item["state"], "prs": proofs, **self._flags_of(flags), **gaps}
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] not in self.AUTO_STATES:
                raise Refused("conflict", f"{key} is {fresh['state']}")
            stable = lambda row: {k: v for k, v in row.items() if k not in ("updated_at", "lease_until", "_merge_project")}
            if stable(fresh) != stable(item):
                raise Refused("conflict", f"{key} changed under the transaction")
            fresh.update({field: pinned[field] for field in ("_merge_project", "_close_head") if field in pinned})
            if self._merged_auto_proofs(fresh) != proofs:
                raise Refused("conflict", "merged_on_default_changed")
            if seated and self._auto_seats(fresh, proofs, now, require_seats) != (witnessed, flags):
                raise Refused("conflict", "review_witness_changed")
            seats = list(dict.fromkeys(model_family(s["model"]) for s in witnessed))
            head = proofs[0]["head"]
            ref = {"sha": head, "merged_head": head, "prs": proofs, "families": seats,
                   "caller": principal.name, "source": "merge-close", "require_seats": require_seats,
                   "landing": landing, **self._flags_of(flags)}
            if not self._deploy_scan_current(fresh, token):
                raise Refused("conflict", "merged_on_default_changed")
            timing.check_io_deadline("merge close evidence")
            done = self._land_merged(principal, fresh, head, seats, witnessed, f"auto-{head[:12]}",
                                     fresh["state"] != "in_review", ("commit", ref, Result(True, "merged on default")),
                                     f"merge close: merged {head}", now, release=True, flags=flags)
            return {**result, "key": key, "state": done["state"], "prs": proofs, "seats": seats,
                    **self._flags_of(flags), **gaps}

    def _live_evidence_missing(self, item, proofs):
        if not has_live_clause(item):
            return False
        stamps = [as_epoch(proof["merged_at"]) for proof in proofs]
        if any(stamp is None for stamp in stamps):
            return True
        return live_evidence_missing(item, self._evidence(item["key"]), max(stamps))

    def _merged_auto_proofs(self, item):
        """``[{pr, head, decision_id: None}]`` of the item's merged work; refuses while a lane is open."""
        git, project = self._adapter("git"), self._merge_project(item)
        proofs = []
        # A Merge-Repo hint only names the repository: the item's own trailers and lanes decide.
        for commit in self._merged_work_commits(item):
            pr, head, date = self._merged_commit_proof(git, project, commit)
            merge = commit.get("merge") or {}
            sha = merge.get("sha")
            if pr is None and not merge and (squash := self._landing_for(project, commit.get("sha"))):
                # A squash merge has no merge commit: the forge names its PR and the head that was reviewed.
                pr, head, date, sha = squash["pr"], squash["head"], squash["merged_at"], commit["sha"]
            if pr is None or not valid_commit_ref(sha) or not valid_commit_ref(head):
                raise Refused("evidence_unverified", "merged_pr_missing")
            if all(p["head"] != head for p in proofs):
                proofs.append({"pr": pr, "head": head, "decision_id": None,
                               "merge_sha": sha, "merged_at": date})
        return proofs

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
