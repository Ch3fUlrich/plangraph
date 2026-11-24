"""Full task context on reads, stale-claim refusal on ``claim`` (TASK-CONTEXT-GUARD, D-3215).

Sessions started work that was already merged, cancelled or overtaken by a
later decision because ``ready``/``show``/``claim`` handed them a thin record.
This mixin adds, per item:

* ``dep_states`` / ``dependents``: every dependency and dependent with its
  state and, when terminal or replaced, why (cancel reason, ``corrected by``);
* ``linked_decisions``: D-ids cited in the item text or naming its key in the
  decisions log; ``superseded_premises`` maps a cited id to the row that
  retired it; ``newer_decisions`` lists rows naming the key that are newer
  than the item's premise (see :meth:`_watermark`) and not cited by it;
* ``last_activity``: the newest event (time, actor, type);
* ``work_exists``: ``Plan-Item`` commits on the default ref and open lanes
  (``None`` = unknown, e.g. no repo configured or git failed);
* ``stale``: ``None`` or the joined reasons (untouched > 7 days, superseded
  premise, newer decision, already merged).

``claim`` refuses an item with a merged ``Plan-Item`` commit or one a newer
decision names, unless ``force_stale`` carries a reason, which is recorded on
the ``claimed`` event.  A cancelled item or dependency is refused as before,
now with its cancel reason.  Unknown facts (no repo, git failure, no
decisions file) never refuse.  The guard reads git and the decisions log
before the write lock: both are outside the keeper, so no lock could hold
them still, and the state checks are repeated under the lock as before.
"""
from __future__ import annotations

import bisect
import json
import re
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from .. import views
from . import context_sources as src, timing
from .brief import ensure_cap, superseded_contract
from .errors import Refused
from .evidence_decisions import decision_book, decisions_naming
from .evidence_pr import landing_record
from .evidence_types import valid_commit_ref
from .store_runs import merged_decision_delivery
from .schema import DEFINITION_EVENTS

__all__ = ["ContextMixin", "STALE_DAYS", "READY_GIT_LIMIT", "FORCE_STALE_MAX"]

STALE_DAYS = 7
_CLAIM_INDEX_WORKERS = 4
#: Legacy public constant; ready now checks every row through the shared index.
READY_GIT_LIMIT = 25
FORCE_STALE_MAX = 500
BRIEF_DECISIONS = 6
BRIEF_STALE_MAX = 240


def _payload(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _num(did):
    return int(did.split("-", 1)[1])


def _graph(index):
    """``(dependents, corrected_by)`` maps over the whole graph, one pass."""
    dependents, corrected = {}, {}
    for other in index.values():
        for dep in other.get("depends_on") or []:
            dependents.setdefault(dep, []).append(other["key"])
        if other.get("corrects"):
            corrected.setdefault(other["corrects"], []).append(other["key"])
    return dependents, corrected


class ContextMixin:
    """Task-context reads and the stale-claim guard."""

    # ------------------------------------------------------------ inputs
    def _inputs(self, index, *, fresh=False):
        """Per-request shared inputs: decision rows, retirements, graph, clock."""
        return {"book": src.decision_book(self._adapter("decisions"), fresh=fresh),
                "graph": _graph(index), "clock": self._decision_clock()}

    def _last_activity_map(self):
        out = {}
        for row in self.conn.execute(
                "SELECT e.key, e.ts, e.actor, e.type FROM events e JOIN"
                " (SELECT key, MAX(seq) AS m FROM events WHERE key IS NOT NULL"
                " GROUP BY key) x ON e.seq = x.m"):
            out[row["key"]] = {"at": row["ts"], "actor": row["actor"],
                               "type": row["type"]}
        return out

    def _last_activity(self, key):
        row = self.conn.execute(
            "SELECT ts, actor, type FROM events WHERE key=? ORDER BY seq DESC"
            " LIMIT 1", (key,)).fetchone()
        return ({"at": row["ts"], "actor": row["actor"], "type": row["type"]}
                if row else None)

    def _reasons(self, keys):
        """``key -> cancel reason`` for the cancelled items among *keys*."""
        keys = sorted(set(keys))
        out = {}
        if not keys:
            return out
        marks = ",".join("?" * len(keys))
        for row in self.conn.execute(      # one query; ascending, so newest wins
                f"SELECT key, payload FROM events WHERE type='cancelled'"
                f" AND key IN ({marks}) ORDER BY seq", keys):
            out[row["key"]] = _payload(row["payload"]).get("reason") or "cancelled"
        return out

    def _decision_clock(self):
        """``{"seqs", "marks", "last"}``: running max D-id over definition events.

        ``marks[i]`` is the highest D-id cited by any item definition up to
        ``seqs[i]``; ``last`` maps a key to its newest definition seq.  Kept on
        the store and rebuilt whenever the (count, max, sum of seq) of definition
        events changes, so a rollback or an out-of-order commit never leaves
        it behind.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(seq) AS m, COALESCE(SUM(seq), 0) AS s"
            f" FROM events WHERE {DEFINITION_EVENTS}").fetchone()
        stamp = (row["n"], row["m"], row["s"])
        clock = getattr(self, "_ctx_clock", None)
        if clock is not None and clock["stamp"] == stamp:
            return clock
        clock = {"seqs": [], "marks": [], "last": {}, "stamp": stamp}
        mark = -1
        for row in self.conn.execute(
                f"SELECT seq, key, payload FROM events WHERE {DEFINITION_EVENTS}"
                " ORDER BY seq"):
            raw = row["payload"]
            ids = src.decision_ids(raw if isinstance(raw, str) else json.dumps(raw))
            if ids:
                mark = max(mark, _num(ids[-1]))
            clock["seqs"].append(row["seq"])
            clock["marks"].append(mark)
            clock["last"][row["key"]] = row["seq"]
        self._ctx_clock = clock
        return clock

    def _watermark(self, item, clock, cited=None):
        """The newest decision the item's premise knew of, as a number.

        The highest D-id cited in the item text; failing that, the decision
        clock at its last definition (the highest D-id any definition had
        cited by then).  ``None`` = unknown, which never refuses.
        """
        cited = src.decision_ids(*self._texts(item)) if cited is None else cited
        if cited:
            return _num(cited[-1])
        seq = clock["last"].get(item["key"])
        if seq is None:
            return None
        mark = clock["marks"][bisect.bisect_right(clock["seqs"], seq) - 1]
        return mark if mark >= 0 else None

    @staticmethod
    def _texts(item):
        return [item.get(name) for name in
                ("title", "description", "done_criteria", "note")]

    def _work(self, item, fetch, fresh_lanes=False):
        gitad = self._adapter("git")
        with timing.outside_request_lock():
            commits = src.plan_item_commits(gitad, item["project"], item["key"], fetch=fetch)
            closed = []
            lanes = src.open_lanes(gitad, item["project"], item["key"], fresh_lanes, closed=closed)
        return {"commits": commits, "lanes": lanes,
                **({"closed_pr_lanes": closed} if closed else {})}

    # ----------------------------------------------------------- context
    def _task_context(self, item, index, *, inputs=None, activity=None,
                      git=True, fetch=False, fresh_lanes=False):
        key = item["key"]
        inputs = inputs or self._inputs(index)
        dependents_map, corrected = inputs["graph"]
        dependents = sorted(dependents_map.get(key, []))
        deps = list(item.get("depends_on") or [])
        reasons = self._reasons([k for k in deps + dependents
                                 if index.get(k, {}).get("state") == "cancelled"])

        def entry(k):
            why = reasons.get(k)
            if corrected.get(k):
                why = (why + "; " if why else "") + "corrected by " + \
                    ",".join(sorted(corrected[k]))
            return {"key": k, "state": index.get(k, {}).get("state"), "reason": why}

        cited = src.decision_ids(*self._texts(item))
        naming = src.decisions_naming(inputs["book"], key)
        mark = self._watermark(item, inputs["clock"], cited)
        newer = [d for d in naming if d not in cited
                 and mark is not None and _num(d) > mark]
        superseded = inputs["book"]["superseded"]
        ctx = {
            "dep_states": [entry(k) for k in deps],
            "dependents": [entry(k) for k in dependents],
            "corrected_by": sorted(corrected.get(key, [])),
            "linked_decisions": sorted(set(cited) | set(naming), key=_num),
            "superseded_premises": {d: superseded[d] for d in cited if d in superseded},
            "newer_decisions": newer,
            "last_activity": (activity.get(key) if activity is not None
                              else self._last_activity(key)),
            "work_exists": (self._work(item, fetch, fresh_lanes) if git
                            else {"commits": None, "lanes": None}),
        }
        if contract := superseded_contract(item):
            ctx["superseded_contract"] = contract
        ctx["stale"] = self._stale_reason(item, ctx)
        return ctx

    def _stale_reason(self, item, ctx):
        why = [ctx["superseded_contract"]] if ctx.get("superseded_contract") else []
        last = ctx.get("last_activity") or {}
        at = self._parse_iso(last.get("at")) if last.get("at") else None
        if at is not None and item.get("state") not in views.TERMINAL:
            days = int((self.now() - at.timestamp()) // 86400)
            if days > STALE_DAYS:
                why.append(f"untouched {days}d")
        for old, new in sorted(ctx["superseded_premises"].items()):
            why.append(f"premise {old} superseded by {new}")
        if ctx["newer_decisions"]:
            why.append("newer decision " + ",".join(ctx["newer_decisions"])
                       + " names it; re-confirm the premise")
        commits = (ctx.get("work_exists") or {}).get("commits")
        if commits and item.get("state") not in views.TERMINAL:
            why.append(f"already merged {commits[0]['sha'][:8]}")
        return "; ".join(why) or None

    def _brief_context(self, brief, item, index):
        """Add ``decisions``/``merged``/``stale`` and re-cap: :func:`ensure_cap`
        only ever shortens description, done criteria and note."""
        ctx = self._task_context(item, index)
        commits = ctx["work_exists"]["commits"]
        if commits:
            brief["merged"] = commits[0]["sha"][:8] + (
                f" PR#{commits[0]['merge']['pr']}"
                if (commits[0].get("merge") or {}).get("pr") else "")
        if ctx["linked_decisions"]:      # newest few; show has them all
            brief["decisions"] = ",".join(ctx["linked_decisions"][-BRIEF_DECISIONS:])
        if ctx["stale"]:
            brief["stale"] = _clip(ctx["stale"], BRIEF_STALE_MAX)
        return ensure_cap(brief)

    # -------------------------------------------------------------- ready
    def _work_indexes(self, projects, *, fresh=False, shared=None):
        """Work indexes of every scanned repository.

        A fresh read (the claim guard) refreshes only *projects* from origin, and
        shares one refresh per repository between the keys of a batch and between
        claims running at the same time; the other repositories serve their last
        built index.
        """
        git = self._adapter("git")
        scan = getattr(git, "scan_projects", None)
        names = {*projects, *(scan() if scan is not None else ())}
        if not fresh:
            return {name: src.work_index_cached(git, name) for name in sorted(names)}
        indexes = {} if shared is None else shared
        missing = tuple(sorted(set(projects) - indexes.keys()))
        if missing:
            indexes.update(timing.share_inflight(
                ("work indexes", id(git), missing), lambda: self._claim_work_indexes(git, missing),
                step=f"work index for {missing[0]}" if len(missing) == 1 else "work indexes"))
        return {name: indexes[name] if name in projects else timing.share_inflight(
                    ("cached work", id(git), name), lambda name=name: src.work_index_cached(git, name),
                    step=f"work index for {name}")
                for name in sorted(names)}

    @staticmethod
    def _claim_work_indexes(git, names):
        groups = {}
        for name in names:
            repo, _ = src._repo_ref(git, name)
            groups.setdefault(repo, []).append(name)

        def load(group):
            return {name: timing.share_inflight(
                ("work", id(git), name),
                lambda: src.work_index(git, name, fresh=True),
                step=f"work index for {name}") for name in group}

        if len(groups) == 1:
            return load(next(iter(groups.values())))
        # Each group has its own checkout. Keep fetches for aliases of the
        # same checkout on one worker, and pass the claim deadline to each worker.
        with ThreadPoolExecutor(max_workers=_CLAIM_INDEX_WORKERS) as pool:
            jobs = [pool.submit(copy_context().run, load, group) for group in groups.values()]
            return {name: index for job in jobs for name, index in job.result().items()}

    @staticmethod
    def _indexed_delivery(indexes, key, project, book):
        """Keep bare IDs local and match qualified deliveries across repositories."""
        work = src.indexed_work(indexes[project], key)
        commits = {commit["sha"]: commit for commit in work["commits"] or ()}
        for name, index in indexes.items():
            if name == project or not index or index.get("origin_stale"):
                continue
            for commit in index["commits"].get(key, ()):
                commits.setdefault(commit["sha"], commit)
        if commits:
            work["commits"] = list(commits.values())[:src.COMMIT_LIMIT]
        elif proof := merged_decision_delivery(indexes, book, key, project):
            work["commits"] = [proof]
        return work

    def _add_decision_reconcile_rows(self, report):
        """Include decision-only merge candidates in the routing preview without applying a close."""
        warmed = self.__dict__.get("_warmed")
        with timing.outside_request_lock():
            book = src.decision_book(self._adapter("decisions"))
        if self.__dict__.get("_warmed") is not warmed:
            raise Refused("conflict", "merge index changed during the watched preview")
        indexes = {}
        for name in report["repos"]:
            if not self._warm(name):
                raise Refused("evidence_unverified", f"merge_index_not_warm: {name}")
            indexes[name] = warmed[name]
        if not book["rows"]:
            return
        listed = report["listed"]
        covered = {row["key"] for row in listed + report["would_close"]}
        for item in self._all_items():
            key, project = item["key"], item["project"]
            if (key in covered or item["state"] not in self.AUTO_STATES
                    or item["kind"] in ("goal", "operator_task")):
                continue
            if proof := merged_decision_delivery(indexes, book, key, project):
                listed.append({"key": key, "state": item["state"],
                               "reason": f"evidence_unverified: decision_merge_only: {proof['source']} {proof['sha']}"})
                report["merged_not_done"] += 1

    def _ready_items(self, project=None, n=10, owner=None, agent=False):
        """Candidate rows with shared claim facts and presentation status."""
        items = self._all_items()
        index = {it["key"]: it for it in items}
        states = {key: item["state"] for key, item in index.items()}
        trains = self._open_claim_trains()
        bases = {}

        def dependencies_ready(item):
            train = self._claim_train(item, states, trains)
            if train:
                bases[item["key"]] = train["base_sha"]
            return train is not False

        rows = [dict(row) for row in views.ready(items, project=project, owner=owner, agent=agent,
                                                dependencies_ready=dependencies_ready)]
        for row in rows:
            if row["key"] in bases:
                row["base_sha"] = bases[row["key"]]
        inputs, activity = self._inputs(index), self._last_activity_map()
        indexes = self._work_indexes({r["project"] for r in rows}) if rows else {}
        unknown_pr_sources = [name for name, entry in indexes.items()
                              if entry and entry.get("prs_unverified")]
        for row in rows:
            ctx = self._task_context(row, index, inputs=inputs,
                                     activity=activity, git=False)
            work_index = indexes[row["project"]]
            if work_index and work_index.get("stale"):
                verified = work_index.get("verified_at")
                row["work_index"] = {
                    "project": row["project"], "status": "stale",
                    "age_seconds": max(0, int(time.time() - verified)) if verified is not None else None,
                    "reason": work_index["reason"]}
            if work_index and work_index.get("refreshing"):
                row["work_index_note"] = {
                    "project": row["project"], "status": "refreshing",
                    "age_seconds": work_index["built_age_s"],
                    "reason": "work index built "
                              f"{work_index['built_age_s']}s ago, refresh running"}
            ctx["work_exists"] = self._indexed_delivery(indexes, row["key"], row["project"], inputs["book"])
            prs = src.indexed_open_pulls(indexes, row["key"], row["project"])
            if prs:
                ctx["work_exists"]["prs"] = prs
            ctx["stale"] = self._stale_reason(row, ctx)
            row["context"] = ctx
            facts = self._claim_staleness(ctx)
            if prs:
                row["prs"] = prs
                row["state"] = "in_review"
                row["ready_status"] = "in_progress"
            elif facts:
                row["reason"] = " | ".join(facts)
                row["ready_status"] = "needs_reconcile"
            elif row.get("work_index") or unknown_pr_sources:
                if not row.get("work_index"):
                    row["work_index"] = {
                        "project": row["project"], "status": "stale", "age_seconds": None,
                        "reason": "open PR inventory unavailable: " + ", ".join(unknown_pr_sources)}
                row["ready_status"] = "unknown"
            else:
                row["ready_status"] = "ready"
        rows.sort(key=lambda r: (r["ready_status"] != "ready",
                                 r["context"]["stale"] is not None))
        # Keep the legacy internal all-rows view for context consumers.
        return rows if n is None else rows[:max(0, int(n))]

    def _ready_groups(self, project=None, n=10, owner=None, agent=False):
        rows = self._ready_items(project=project, n=None, owner=owner, agent=agent)
        groups = {"items": [], "needs_reconcile": [], "in_progress": [],
                  "unknown": [], "warnings": []}
        for row in rows:
            warning = row.get("work_index") or row.get("work_index_note")
            if warning and not any(w["project"] == warning["project"]
                                   for w in groups["warnings"]):
                groups["warnings"].append(warning)
            group = "items" if row["ready_status"] == "ready" else row["ready_status"]
            groups[group].append(row)
        if n is not None:
            groups["items"] = groups["items"][:max(0, int(n))]
        return groups

    def _ready_text(self, project=None, n=10, owner=None, agent=False):
        from .fmt import ready_text
        groups = self._ready_groups(project=project, n=n, owner=owner, agent=agent)
        return ready_text(groups["items"], unblocks=views.unblocks(self._all_items()),
                          server_items=True, needs_reconcile=groups["needs_reconcile"],
                          in_progress=groups["in_progress"], unknown=groups["unknown"],
                          warnings=groups["warnings"])

    # -------------------------------------------------------------- claim
    @staticmethod
    def _force_stale_reason(value):
        """``None`` or the stripped reason; anything else is a validation refusal."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise Refused("validation", "force_stale must be a non-empty reason string")
        if len(value.strip()) > FORCE_STALE_MAX:
            raise Refused("validation",
                          f"force_stale reason is over {FORCE_STALE_MAX} chars")
        return value.strip()

    def _claim_guard(self, item, reason, *, shared=None, stale_checks=True):
        """Refuse a stale claim with the facts, or return the override record."""
        reason = self._force_stale_reason(reason)
        project = item["project"]
        shared = {} if shared is None else shared
        with timing.outside_request_lock():
            indexes = self._work_indexes({project}, fresh=True, shared=shared.setdefault("indexes", {}))
        prs = src.indexed_open_pulls(indexes, item["key"], project)
        if not prs and reason is None:
            prs = src.indexed_open_pulls(indexes, item["key"], project, cached=True)
        if prs:
            raise Refused("bad_state", f"{item['key']} carried by "
                          + ", ".join(f"#{number}" for number in prs))
        unknown = [name for name, entry in indexes.items()
                   if entry and entry.get("prs_unverified")]
        if not stale_checks:
            adapter = self._adapter("decisions")
            with timing.outside_request_lock():
                book = src.decision_book(adapter)
            work = self._indexed_delivery(indexes, item["key"], project, book)
            facts = []
            if work["commits"]:
                head = work["commits"][0]
                facts.append(f"already merged: {head.get('source', 'Plan-Item commit')} {head['sha'][:8]}")
            override = self._stale_override(item["key"], reason, facts)
            if unknown:
                raise Refused("bad_state", "open PR inventory unavailable: " + ", ".join(unknown))
            return override
        index = self._item_neighborhood(item)
        if "clock" not in shared:
            shared["clock"] = self._decision_clock()
        timing.check_io_deadline("decision clock")
        try:
            if "book" not in shared:
                adapter = self._adapter("decisions")
                with timing.outside_request_lock():
                    shared["book"] = timing.share_inflight(
                        ("decisions", id(adapter)),
                        lambda: src.decision_book(adapter, fresh=True),
                        step="decision book",
                    )
        except src.DecisionsUnreadable:
            timing.check_io_deadline("decision book")
            path = getattr(self._adapter("decisions"), "_path", None)
            raise Refused("bad_state", f"cannot read decisions file: {path}") from None
        ctx = self._task_context(item, index, inputs={
            "graph": _graph(index), "book": shared["book"], "clock": shared["clock"]}, git=False)
        timing.check_io_deadline("task context")
        work_index = indexes[project]
        ctx["work_exists"] = self._indexed_delivery(indexes, item["key"], project, shared["book"])
        facts = self._claim_staleness(ctx)
        if work_index is not None and work_index.get("stale"):
            facts.append("work index unverified: " + work_index["reason"])
        if unknown:
            facts.append("work index unverified: open PR inventory unavailable: " + ", ".join(unknown))
        return self._stale_override(item["key"], reason, facts)

    # ----------------------------------------------- automatic close: landing evidence
    #: The operator decision (a ``D-NNN`` row) that stands behind every automatic merge close;
    #: the keeper config sets it. It is recorded in the close evidence, never required to close.
    merge_close_decision = None
    _LANDING_MISS_S = 600

    def _landing_for(self, project, sha):
        """The forge's cached record of the landing at *sha*, or ``None`` when it proves nothing.

        A landing never changes, so a record is kept for good; "no merged PR" is kept for
        ``_LANDING_MISS_S``. The forge is asked outside the keeper lock (``warm_merge_scan``);
        a frozen run reads only what is cached, and a forge failure is retried on the next run.
        """
        cache = self.__dict__.setdefault("_landings", {})
        found = cache.get((project, sha))
        now = self.now()
        if found is not None and (found[0] is not None or now - found[1] < self._LANDING_MISS_S):
            return found[0]
        ci, git = self._adapter("ci"), self._adapter("git")
        if (self.__dict__.get("_frozen_lanes") or not valid_commit_ref(sha)
                or not callable(getattr(ci, "merged_pr_for", None)) or git is None):
            return None
        try:
            record = landing_record(ci, project, sha, git.repo_ref(project))
        except Exception:  # noqa: BLE001 - an unreadable forge proves nothing
            return None
        cache[(project, sha)] = (record, now)
        return record

    def _warm_landings(self, project, commits):
        """Ask the forge for each landing of *commits*, outside the keeper lock."""
        for commit in commits:
            self._landing_for(project, (commit.get("merge") or {}).get("sha") or commit.get("sha"))

    def _decision_snapshot(self, keys):
        """The decisions the landing evidence reads, captured once: ``None`` when unreadable.

        ``{"book", "texts", "standing"}``: the decision book, the text of every row naming one of
        *keys*, and whether the standing decision exists. The scan takes it outside the keeper lock
        (the document may be read from origin); a frozen close reads this copy and never the file.
        """
        decisions = self._adapter("decisions")
        if decisions is None:
            return None
        snapshot = getattr(decisions, "snapshot", None)   # one read of the document serves every lookup below
        try:
            with snapshot() if callable(snapshot) else nullcontext():
                # Strict read: the plain book turns an unreadable source into an empty one.
                book = decision_book(decisions, fresh=True)
                texts = {did: decisions.text(did) for key in keys for did in decisions_naming(book, key)}
                standing = bool(decisions.has(self.merge_close_decision)) if self.merge_close_decision else None
        except Exception:  # noqa: BLE001 - an unreadable document proves and names nothing
            return None
        return {"book": book, "texts": texts, "standing": standing}

    def _snapshot_landing_decisions(self, candidates):
        self.__dict__["_landing_decisions"] = self._decision_snapshot([key for key, _ in candidates])

    def _landing_evidence(self, item, proofs, now):
        """Standing authority, pre-merge required checks and GO rows of an automatic close.

        A gap is a flag, never a refusal: the merge itself is the proved landing.
        """
        flags, landings, project = {}, [], self._merge_project(item)
        snapshot = (self.__dict__.get("_landing_decisions") if self.__dict__.get("_frozen_lanes")
                    else self._decision_snapshot([item["key"]]))
        decision = self.merge_close_decision
        authority = None
        if decision:
            known = bool(snapshot and snapshot["standing"])
            authority = {"decision": decision, "verified": known}
            if not known:
                flags["standing_authority_unverified"] = decision
        else:
            flags["standing_authority_missing"] = True
        unverified = []
        for proof in proofs:
            record = self._landing_for(project, proof["merge_sha"])
            checks = record["checks"] if record else None
            landings.append({"pr": proof["pr"], "merge_sha": proof["merge_sha"], "head": proof["head"],
                             "merged_at": record["merged_at"] if record else proof["merged_at"],
                             "checks": checks})
            if checks is None:
                unverified.append({"pr": proof["pr"], "detail": "forge_record_unavailable"})
            elif not checks["verified"]:
                unverified.append({"pr": proof["pr"], "detail": checks["detail"]})
        if unverified:
            flags["required_checks_unverified"] = unverified
        go_rows = self._landing_go_rows(item, proofs, snapshot)
        if snapshot is None:   # an unreadable decisions document is not a missing row
            flags["go_rows_unverified"] = True
        elif missing := [p["pr"] for p in proofs if p["pr"] not in {row["pr"] for row in go_rows}]:
            flags["go_row_missing"] = {"prs": missing}
        return {"authority": authority, "landings": landings, "go_rows": go_rows, "flags": flags}

    def _landing_go_rows(self, item, proofs, snapshot):
        """``[{decision_id, pr}]``: the GO rows that name this item and bind a landed PR or head."""
        from .store_waiver import _go_binding as go_binding, _go_statement as go_statement, _names_item as names_item

        projects, rows = {self._merge_project(item), item["project"]}, []   # a Merge-Repo item may be named by either
        for did in decisions_naming(snapshot["book"], item["key"]) if snapshot else ():
            statement = go_statement(snapshot["texts"].get(did))
            if statement is None or not names_item(statement, item["key"]):
                continue
            pr = next((found for found, _ in (go_binding(statement, p) for p in sorted(projects)) if found), None)
            bound = re.match(r"^GO\s+`?([0-9a-fA-F]{8,64})(?![\w])", statement)
            rows += [{"decision_id": did, "pr": p["pr"]} for p in proofs
                     if p["pr"] == pr or (pr is None and bound and p["head"].startswith(bound[1].lower()))]
        return rows

    @staticmethod
    def _stale_override(key, reason, facts):
        if not facts:
            return None
        if reason is None:
            raise Refused("bad_state", f"{key} stale: " + " | ".join(facts)
                          + " (override: force_stale=<reason>, audited)")
        return {"reason": reason, "overrode": facts}

    @staticmethod
    def _claim_staleness(ctx):
        """Shared refusal facts for claim and ready; informational age is separate."""
        facts = [ctx["superseded_contract"]] if ctx.get("superseded_contract") else []
        commits = ctx["work_exists"]["commits"]
        lanes = ctx["work_exists"]["lanes"]
        if commits and (not lanes or commits[0].get("source")):
            head = commits[0]
            merge = head.get("merge") or {}
            facts.append(
                f"already merged: {head.get('source', 'Plan-Item commit')} {head['sha'][:8]} ({head['date']})"
                + (f" via PR #{merge['pr']}" if merge.get("pr") else "")
                + " is on the default branch; attach evidence/submit instead")
        if ctx["newer_decisions"]:
            facts.append("premise outdated: " + ",".join(ctx["newer_decisions"])
                         + " names this item after its definition; re-confirm"
                         " (edit the item to cite it) first")
        return facts

    def _not_open_facts(self, item, index):
        """Why a non-claimable item is not claimable, with reasons (for refusals)."""
        ctx_reasons = self._reasons([item["key"]] + [
            d for d in item.get("depends_on") or []
            if index.get(d, {}).get("state") == "cancelled"])
        bits = []
        if item["key"] in ctx_reasons:
            bits.append(f"cancelled: {ctx_reasons[item['key']]}")
        for dep in item.get("depends_on") or []:
            state = index.get(dep, {}).get("state")
            if state != "done":
                bits.append(f"dep {dep}={state}"
                            + (f" ({ctx_reasons[dep]})" if dep in ctx_reasons else ""))
        return "; ".join(bits)
