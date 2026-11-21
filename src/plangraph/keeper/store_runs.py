"""Spawner ``record_run`` verb for :class:`plangraph.keeper.store.Store`.

The spawner records which models ran on an item (``run_recorded`` events);
``show``/``snapshot`` expose the cached columns plus derived fields.
"""
from __future__ import annotations

import contextlib
import json
import re
import time

from . import context_sources
from .errors import Refused
from .evidence_git import plan_items
from .evidence_runs import RunRecordAdapter
from .evidence_types import valid_commit_ref

__all__ = ["RunsMixin", "normalise_run"]

_REVIEW_RUN = re.compile(r"Review-Run:[ \t]+(\S+)[ \t]+(run-\S+)[ \t]+([0-9a-f]{40})[ \t]*")
_WARM_S = 60  # how long a warm scan may be read under the request lock
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_PHASES = ("started", "finished")
_VERDICTS = ("pass", "fail", "invalid", None)
_TOP_KEYS = frozenset({"run_id", "phase", "rc", "writer", "seats", "item_key"})
_WRITER_KEYS = frozenset({"model", "family", "source"})
_SEAT_KEYS = frozenset({"model", "verdict", "run_id"})
_UTC_DATE_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|\+00:00))?\Z")


def _operator_authority(row):
    """The decisions log grants authority through comma/plus-separated BY tokens."""
    return "operator" in {token.strip().lower() for token in re.split(r"[,+]", row.get("by", ""))}


def _decision_date(row):
    """Logs without a DATE column must state a unique Date in the decision cell."""
    values = re.findall(r"(?:^|[;\s])Date: +([^;\s]+)", row.get("decision", ""))
    markers = re.findall(r"(?:^|[;\s])Date:(?=\s|$)", row.get("decision", ""))
    dates = [value for value in [row.get("date"), *values] if value]
    if len(markers) != len(values) or len(dates) != 1 or not _UTC_DATE_RE.fullmatch(dates[0]):
        return None
    return dates[0]


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _check_run_id(value, where="run_id"):
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise Refused("validation", f"{where} must match [A-Za-z0-9._:-]{{1,128}}")
    return value


def normalise_run(run) -> dict:
    """Validate *run* and return the normalised payload (refuses on violation)."""
    if not isinstance(run, dict):
        raise Refused("validation", "run must be an object")
    unknown = set(run) - _TOP_KEYS
    if unknown:
        raise Refused("validation", f"unknown run keys: {sorted(unknown)}")
    if "run_id" not in run:
        raise Refused("validation", "run_id is required")
    if "phase" not in run:
        raise Refused("validation", "phase is required")
    run_id = _check_run_id(run["run_id"])
    phase = run["phase"]
    if phase not in _PHASES:
        raise Refused("validation", "phase must be one of ['started', 'finished']")
    rc = run.get("rc")
    if rc is not None and (isinstance(rc, bool) or not isinstance(rc, int)):
        raise Refused("validation", "rc must be an int or null")
    writer = run.get("writer")
    if writer is not None:
        if not isinstance(writer, dict):
            raise Refused("validation", "writer must be an object or null")
        unknown = set(writer) - _WRITER_KEYS
        if unknown:
            raise Refused("validation", f"unknown writer keys: {sorted(unknown)}")
        model = writer.get("model")
        if not isinstance(model, str) or not 1 <= len(model) <= 128:
            raise Refused("validation", "writer.model must be a string<=128")
        family = writer.get("family")
        if family is not None and (not isinstance(family, str) or len(family) > 64):
            raise Refused("validation", "writer.family must be a string<=64 or null")
        source = writer.get("source")
        if source is not None and (not isinstance(source, str) or len(source) > 32):
            raise Refused("validation", "writer.source must be a string<=32 or null")
        writer = {"model": model, "family": family, "source": source}
    seats = run.get("seats")
    if seats is not None:
        if not isinstance(seats, list) or len(seats) > 8:
            raise Refused("validation", "seats must be a list<=8 or null")
        normed = []
        for seat in seats:
            if not isinstance(seat, dict):
                raise Refused("validation", "seats entries must be objects")
            unknown = set(seat) - _SEAT_KEYS
            if unknown:
                raise Refused("validation", f"unknown seat keys: {sorted(unknown)}")
            model = seat.get("model")
            if not isinstance(model, str) or not 1 <= len(model) <= 128:
                raise Refused("validation", "seat.model must be a string<=128")
            verdict = seat.get("verdict")
            if verdict not in _VERDICTS:
                raise Refused("validation",
                              "seat.verdict must be one of pass/fail/invalid/null")
            entry = {"model": model, "verdict": verdict}
            if seat.get("run_id") is not None:
                entry["run_id"] = _check_run_id(seat["run_id"], "seat.run_id")
            normed.append(entry)
        seats = normed
    item_key = run.get("item_key")
    if item_key is not None and not isinstance(item_key, str):
        raise Refused("validation", "item_key must be a string or null")
    return {"run_id": run_id, "phase": phase, "rc": rc,
            "writer": writer, "seats": seats, "item_key": item_key}


class RunsMixin:
    """The spawner-only ``record_run`` verb."""

    def _decisions_snapshot(self, item, now):
        """One read and parse of the decisions document for the block (no-op when unsupported)."""
        decisions = self._ctx(item, now).decisions
        snapshot = getattr(decisions, "snapshot", None)
        return snapshot() if callable(snapshot) else contextlib.nullcontext()

    def _merged_cited_parts(self, item, heads):
        """``{merged head: {part head: [run ids]}}`` a landed batch cites for the item.

        The merge commit that landed a head lists ``Review-Run: <key> <run-id> <part head>``
        per keeper-bound part review. A part head counts when it is reachable from that
        merge's second parent, outside its first-parent history, and a commit of its own range
        carries the item's parsed ``Plan-Item`` trailer; the record itself is still verified
        by the witness adapter, so a citation only points at it.
        """
        git, project = self._adapter("git"), self._merge_project(item)
        repo, ref, key = git.repo_path(project), git.repo_ref(project), item["key"]
        names = {key, key.split(":", 1)[-1]}
        valid_run = RunRecordAdapter(None).valid_run_id
        cited = {}
        for head in heads:
            merge = git.merge_commit_of(repo, head, ref)
            if not merge or not valid_commit_ref(merge[0]):
                continue
            body = git._run(repo, ["show", "-s", "--format=%B", "--end-of-options", merge[0]]).stdout
            for line in body.splitlines():
                found = _REVIEW_RUN.fullmatch(line)
                if not found or found[1] != key or not valid_run(found[2]):
                    continue
                part = found[3]
                inside = git._run(repo, ["merge-base", "--is-ancestor", "--end-of-options", part, f"{merge[0]}^2"])
                listed = git._run(repo, ["log", "--format=%B%x1d", "--end-of-options", part, f"^{merge[0]}^1"])
                if inside.returncode != 0 or listed.returncode != 0:
                    continue
                values = {v for record in listed.stdout.split("\x1d") for v in plan_items(record)}
                if names & values:
                    cited.setdefault(head, {}).setdefault(part, []).append(found[2])
            if head in cited and not self._parts_cover_merge((git, repo), merge[0], names, list(cited[head])):
                del cited[head]  # the batch brought more of the item than the cited parts: the head needs its own seat
        return cited

    @staticmethod
    def _parts_cover_merge(git_repo_args, merge, names, parts):
        """True when every commit (nested merges too) the merge introduced for the item lies at or under a cited part head."""
        git, repo = git_repo_args
        listed = git._run(repo, ["log", "--format=%H%x1f%B%x1d", "--end-of-options",
                                 f"{merge}^2", f"^{merge}^1"])
        if listed.returncode != 0:
            return False
        for record in listed.stdout.split("\x1d"):
            sha, _, body = record.strip("\n").partition("\x1f")
            if sha and names & set(plan_items(body)) and not any(
                    git._run(repo, ["merge-base", "--is-ancestor", "--end-of-options", sha, part]).returncode == 0
                    for part in parts):
                return False
        return True

    def merge_candidates(self):
        """``(key, project)`` of every item the automatic close may take: the input of :meth:`warm_merge_scan`."""
        return [(item["key"], item["project"]) for item in self._all_items()
                if item["state"] in self.AUTO_STATES and item["kind"] not in ("goal", "operator_task")]

    def warm_merge_scan(self, candidates=()):
        """Fetch, index and look up the hosted lanes of the scanned repositories; the API calls it outside its lock.

        The run that follows reads the warm answers (see :meth:`_warm`) instead of asking git or the
        forge under the lock.
        """
        git = self._adapter("git")
        scan = getattr(git, "scan_projects", None)
        self.__dict__["_warmed"], self.__dict__["_lanes"] = {}, {}   # a failed refresh must not keep an old snapshot
        for project in list(scan()) if callable(scan) else []:
            if self._refresh_merged_default_ref(git, project):
                index = context_sources.work_index(git, project, fresh=True)
                if index is not None and not index.get("origin_stale"):
                    for key, item_project in candidates:
                        if context_sources.indexed_work(index, key)["commits"]:
                            context_sources.open_lanes(git, project, key, cache=self.__dict__["_lanes"])
                    self.__dict__["_warmed"][project] = time.monotonic()

    def _warm(self, project):
        return time.monotonic() - self.__dict__.get("_warmed", {}).get(project, -1e9) < _WARM_S

    def _merged_open_prs(self, item, decision_id, head, now):
        try:
            return self._merged_open_prs_checked(item, decision_id, head, now)
        except Refused:
            raise
        except Exception:
            raise Refused("evidence_unverified", "merged_pr_proof_unavailable") from None

    def _merged_seat_waiver(self, item, decision_id, waiver_id, prs, now):
        from .evidence_types import as_epoch
        check = "decision_id"
        try:
            if not isinstance(waiver_id, str) or not re.fullmatch(r"D-[0-9]+", waiver_id):
                raise ValueError("operator decision id required")
            decisions = self._ctx(item, now).decisions
            check = "unique_row_and_columns"
            row = decisions.fields(waiver_id)
            if not row:
                raise ValueError("missing or ambiguous waiver row")
            check = "operator_authority"
            if not _operator_authority(row):
                raise ValueError("OPERATOR required")
            check = "waiver_class_and_verification"
            if (not re.match(r"^Waiver:\s*merged-without-witnesses(?=$|[\s;,.])", row.get("decision", ""))
                    or not self._verify_evidence(item, "decision", {"id": waiver_id}, now).ok):
                raise ValueError("operator waiver is not authorized")
            # The list ends at the next field/semicolon; prose outside it has
            # no authority. Split only on spaces: commas are not delimiters.
            scopes = re.findall(r"(?:^|;|\s)Waives-seats: +([^;\n]+)", row.get("decision", ""))
            tokens = []
            for scope in scopes:
                scope = re.split(r" +[A-Za-z][A-Za-z-]*: +", scope, maxsplit=1)[0]
                tokens.extend(scope.strip().split(" "))
            if item["key"] not in tokens:
                raise Refused("evidence_unverified", "operator_seat_waiver_scope_mismatch")
            if self._card_requires_non_anthropic_seats(item):
                raise Refused("coverage", "insufficient_non_anthropic_seat_families: need 2")
            git = self._adapter("git")
            project = self._merge_project(item)
            check = "historical_cutoff"
            cutoffs = re.findall(r"(?:^|[;\s])Cutoff:", row.get("decision", ""))
            if cutoffs:
                values = re.findall(r"(?:^|[;\s])Cutoff: +([^;\s]+)", row["decision"])
                if len(cutoffs) != 1 or len(values) != 1 or not _UTC_DATE_RE.fullmatch(values[0]):
                    raise ValueError("invalid row cutoff")
                cutoff = as_epoch(values[0])
                row_date = as_epoch(_decision_date(row))
                if cutoff is None or row_date is None:
                    raise ValueError("invalid row cutoff or date")
                if cutoff > row_date:
                    raise Refused("evidence_unverified", "operator_seat_waiver_cutoff_after_row")
            else:
                cutoff = as_epoch(decisions.seat_waiver_cutoff())
            if cutoff is None:
                raise ValueError("invalid historical cutoff")
            for proof in prs:
                go_date = as_epoch(_decision_date(decisions.fields(proof["decision_id"])))
                merge = git.merge_commit_of(git.repo_path(project), proof["head"],
                                             git.repo_ref(project))
                proc = git._run(git.repo_path(project),
                                ["show", "-s", "--format=%cI", "--end-of-options", merge[0]]) if merge else None
                merge_date = as_epoch(proc.stdout.strip()) if proc and proc.returncode == 0 else None
                if merge_date is None:
                    raise Refused("evidence_unverified", "operator_seat_waiver_merge_date_missing")
                if merge_date >= cutoff:
                    raise Refused("evidence_unverified", "operator_seat_waiver_merge_at_or_after_cutoff")
                if go_date is None or go_date >= cutoff:
                    raise Refused("evidence_unverified", "operator_seat_waiver_go_date_ineligible")
            return {"class": "merged-without-witnesses", "decision_id": waiver_id, "scope": tokens,
                    "cutoff": cutoff}
        except Refused:
            raise
        except Exception:
            raise Refused("evidence_unverified", f"operator_seat_waiver_invalid_or_ineligible: {check}") from None

    @staticmethod
    def _refresh_merged_default_ref(git, project):
        """Complete remote history needs a successful fetch, never a throttled one."""
        from .evidence_git import valid_ref
        try:
            repo, ref = git.repo_path(project), git.repo_ref(project)
            if not repo or not valid_ref(ref):
                return False
            if "/" not in ref:
                return True  # configured local refs are authoritative locally
            remote, branch = ref.split("/", 1)
            if not valid_ref(remote) or not valid_ref(branch):
                return False
            return git._run(repo, ["fetch", "--quiet", "--", remote,
                                  f"+refs/heads/{branch}:refs/remotes/{ref}"],
                            timeout=20).returncode == 0
        except Exception:
            return False

    def _merged_go_scope_matches(self, decision_id, item, now, *, require_key=True, decisions=None):
        """Additional PR GOs must name this project and this item explicitly."""
        decisions = decisions or self._ctx(item, now).decisions
        rows = decisions._rows_strict().get(decision_id, ())
        if len(rows) != 1:
            return False
        cells = [c.strip() for c in rows[0].split("|")[1:]]
        if len(cells) < 3 or cells[1] != item["project"]:
            return False
        text = cells[2]
        keys = re.findall(r"(?<![\w:-])[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_.-]+", text)
        if keys:
            return item["key"] in keys
        if not require_key:
            return True  # legacy head-only GO; the caller selected this row
        bare = item["key"].split(":", 1)[1]
        return re.search(r"(?<![\w:-])" + re.escape(bare) + r"(?![\w:-])", text) is not None

    def _merged_open_prs_checked(self, item, decision_id, head, now):
        from . import context_sources
        from .evidence_types import valid_commit_ref
        git = self._adapter("git")
        project = self._merge_project(item)
        if not self._refresh_merged_default_ref(git, project):
            raise Refused("evidence_unverified", "merged_default_ref_refresh_failed")
        if context_sources.open_lanes(git, project, item["key"]) != []:
            raise Refused("evidence_unverified", "unmerged_work_or_lanes_unknown")
        commits = context_sources.plan_item_commits(
            git, project, item["key"], fetch=False, limit=None)
        from .store_waiver import _go_binding, _go_statement, _strip_prefix
        go_pr, go_head = None, None
        try:
            text = _strip_prefix(self._ctx(item, now).decisions.text(decision_id))
            go_pr, go_head = _go_binding(_go_statement(text), item["project"])
        except Exception:  # noqa: BLE001 - unreadable GO row falls back to None
            pass
        named = self._named_merge_proof(item)
        proofs = ({named["head"]: {"pr": named["pr"], "head": named["head"]}}
                  if named and named["pr"] else {})
        repo = git.repo_path(project)
        ref = git.repo_ref(project)
        for commit in commits or ():
            merge = commit.get("merge") or {}
            pr = merge.get("pr")
            has_subject_pr = type(pr) is int and pr > 0
            if not has_subject_pr and (
                    type(go_pr) is not int or go_pr <= 0 or not valid_commit_ref(go_head)):
                raise Refused("evidence_unverified", "merged_pr_missing")
            merged = git.merge_commit_of(repo, commit["sha"], ref)
            proc = git._run(repo, ["show", "-s", "--format=%P", "--end-of-options", merged[0]]) if merged else None
            parents = proc.stdout.strip().split() if proc and proc.returncode == 0 else []
            if len(parents) != 2 or not all(valid_commit_ref(p) for p in parents):
                raise Refused("evidence_unverified", "merged_pr_head_missing")
            if has_subject_pr:
                proofs[parents[1]] = {"pr": pr, "head": parents[1]}
            elif context_sources._head_introduced_by_merge(git, repo, parents, go_head):
                proof_head = go_head if go_head == head else parents[1]
                proofs[proof_head] = {"pr": go_pr, "head": proof_head}
            else:
                raise Refused("evidence_unverified", "merged_pr_missing")
        if proofs and head not in proofs:
            # A GO row bound to its own merged PR (merged_go_row) may name a head
            # the trailer search did not reach; that PR is proof in its own right.
            bound = context_sources.pr_merge_of(
                git, project, head, fallback_pr=go_pr if head == go_head else None)
            if bound:
                proofs[head] = {"pr": bound["pr"], "head": head}
        if not proofs or head not in proofs:
            raise Refused("evidence_unverified", "merged_pr_missing")
        with self._decisions_snapshot(item, now):
            if not self._merged_go_scope_matches(decision_id, item, now, require_key=False):
                raise Refused("evidence_unverified",
                              f"merged_pr_go_scope_mismatch: {item['key']} in {decision_id}")
            proofs[head]["decision_id"] = decision_id
            self._merged_scan_go_rows(item, proofs, now)
        if any("decision_id" not in p for p in proofs.values()):
            raise Refused("evidence_unverified", "merged_pr_go_missing")
        return sorted(proofs.values(), key=lambda p: (p["pr"], p["head"]))

    @staticmethod
    def _go_row_dated(decisions, did):
        try:
            return _decision_date(decisions.fields(did)) is not None
        except Exception:  # noqa: BLE001 - an unreadable date never counts as dated
            return False

    def _merged_scan_go_rows(self, item, proofs, now):
        """Give every merged head an accepted GO row of this item (one document read).

        Per head the first accepted row in sorted id order wins unless it is
        undated and a later accepted row (a larger id) carries a date: the seat waiver
        compares that date with its cutoff, and an old undated row can never
        be edited into a dated one.
        """
        decisions = self._ctx(item, now).decisions
        dated = {h: self._go_row_dated(decisions, p["decision_id"])
                 for h, p in proofs.items() if "decision_id" in p}
        for did in sorted(decisions.decisions()):
            if all(dated.get(h) for h in proofs):
                break
            if not self._merged_go_scope_matches(did, item, now, decisions=decisions):
                continue
            try:
                other_head, _ = self._merged_go_proof(item, did, now, all_work=True)
            except Refused:
                continue
            if other_head not in proofs or dated.get(other_head):
                continue
            row_dated = self._go_row_dated(decisions, did)
            held = proofs[other_head].get("decision_id")
            if held is None or (row_dated and int(did[2:]) > int(held[2:])):
                proofs[other_head]["decision_id"] = did
                dated[other_head] = row_dated

    def _record_merged_seats(self, principal, item, decision_id, seats, now):
        """Mirror a verified historical seat bundle while holding the close lock."""
        seats = [{k: s[k] for k in ("model", "verdict", "run_id")} for s in seats]
        run_id = f"merged-{decision_id}"
        self._append(principal, item["key"], "run_recorded", {
            "run_id": run_id, "phase": "finished", "rc": 0, "seats": seats}, now)
        ids = json.loads(item.get("run_ids") or "[]")
        ids = list(dict.fromkeys([*ids, run_id, *(s["run_id"] for s in seats)]))
        models = [{"model": s["model"], "verdict": s["verdict"]} for s in seats]
        self.conn.execute("UPDATE items SET run_ids=?, seat_models=? WHERE key=?",
                          (_dump(ids), _dump(models), item["key"]))

    def record_run(self, principal, key, run):
        return self._run(principal, "record_run", key,
                         lambda now: self._record_run(principal, key, run, now))

    def _has_run(self, key, run_id, phase) -> bool:
        for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='run_recorded'",
                (key,)):
            try:
                payload = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                continue
            if (isinstance(payload, dict) and payload.get("run_id") == run_id
                    and payload.get("phase") == phase):
                return True
        return False

    def _record_run(self, principal, key, run, now):
        self._require_role(principal, "spawner")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}")
        normed = normalise_run(run)
        run_id, phase = normed["run_id"], normed["phase"]
        if self._has_run(key, run_id, phase):
            return {"key": key, "run_id": run_id, "phase": phase,
                    "duplicate": True}
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] not in ("claimed", "in_review"):
                raise Refused("conflict",
                              f"{fresh['key']} is {fresh['state']}")
            if self._has_run(key, run_id, phase):
                return {"key": key, "run_id": run_id, "phase": phase,
                        "duplicate": True}
            self._append(principal, key, "run_recorded", normed, now)
            try:
                have = json.loads(fresh.get("run_ids")) if fresh.get("run_ids") else []
            except (ValueError, TypeError):
                have = []
            if not isinstance(have, list):
                have = []
            seen = set(have)
            ordered = list(have)
            cands = [run_id]
            if isinstance(normed.get("seats"), list):
                for seat in normed["seats"]:
                    if seat.get("run_id") is not None:
                        cands.append(seat["run_id"])
            for cand in cands:
                if cand not in seen:
                    seen.add(cand)
                    ordered.append(cand)
            run_ids_text = _dump(ordered)
            writer_model = fresh.get("writer_model")
            if normed.get("writer") is not None:
                writer_model = normed["writer"]["model"]
            seat_models_text = fresh.get("seat_models")
            if normed.get("seats") is not None:
                seat_models_text = _dump(
                    [{"model": s["model"], "verdict": s["verdict"]}
                     for s in normed["seats"]])
            self.conn.execute(
                "UPDATE items SET run_ids=?, writer_model=?, seat_models=?,"
                " updated_at=? WHERE key=?",
                (run_ids_text, writer_model, seat_models_text,
                 self._iso(now), key))
        try:
            seat_models = json.loads(seat_models_text) if seat_models_text else None
        except (ValueError, TypeError):
            seat_models = None
        return {"key": key, "run_id": run_id, "phase": phase,
                "writer_model": writer_model, "seat_models": seat_models,
                "run_ids": ordered}
