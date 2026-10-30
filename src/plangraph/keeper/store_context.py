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

from .. import views
from . import context_sources as src
from .brief import ensure_cap
from .errors import Refused
from .fmt import ready_context_rows

__all__ = ["ContextMixin", "STALE_DAYS", "READY_GIT_LIMIT", "FORCE_STALE_MAX"]

STALE_DAYS = 7
#: ``ready`` runs the git check on at most this many rows.
READY_GIT_LIMIT = 25
FORCE_STALE_MAX = 500
BRIEF_DECISIONS = 6
BRIEF_STALE_MAX = 240
_DEF_TYPES = ("item_created", "item_defined")


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
    def _inputs(self, index):
        """Per-request shared inputs: decision rows, retirements, graph, clock."""
        return {"book": src.decision_book(self._adapter("decisions")),
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
            " FROM events WHERE type IN (?,?)", _DEF_TYPES).fetchone()
        stamp = (row["n"], row["m"], row["s"])
        clock = getattr(self, "_ctx_clock", None)
        if clock is not None and clock["stamp"] == stamp:
            return clock
        clock = {"seqs": [], "marks": [], "last": {}, "stamp": stamp}
        mark = -1
        for row in self.conn.execute(
                "SELECT seq, key, payload FROM events WHERE type IN (?,?)"
                " ORDER BY seq", _DEF_TYPES):
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

    def _work(self, item, fetch):
        gitad = self._adapter("git")
        return {"commits": src.plan_item_commits(gitad, item["project"], item["key"],
                                                 fetch=fetch),
                "lanes": src.open_lanes(gitad, item["project"], item["key"])}

    # ----------------------------------------------------------- context
    def _task_context(self, item, index, *, inputs=None, activity=None,
                      git=True, fetch=False):
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
            "work_exists": (self._work(item, fetch) if git
                            else {"commits": None, "lanes": None}),
        }
        ctx["stale"] = self._stale_reason(item, ctx)
        return ctx

    def _stale_reason(self, item, ctx):
        why = []
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
    def _ready_items(self, project=None, n=10, owner=None, agent=False):
        """``views.ready`` rows (copies) with ``context``; stale rows sort last.

        The git check runs on the first :data:`READY_GIT_LIMIT` rows of the
        whole ready list (after a first stale sort on the keeper-only facts)
        BEFORE the cut to *n*, so a merged row cannot crowd out a fresh one.
        """
        items = self._all_items()
        index = {it["key"]: it for it in items}
        rows = [dict(row) for row in
                views.ready(items, project=project, owner=owner, agent=agent)]
        inputs, activity = self._inputs(index), self._last_activity_map()
        for row in rows:
            row["context"] = self._task_context(row, index, inputs=inputs,
                                                activity=activity, git=False)
        rows.sort(key=lambda r: r["context"]["stale"] is not None)  # stable
        for row in rows[:READY_GIT_LIMIT]:
            ctx = row["context"]
            ctx["work_exists"] = self._work(row, fetch=False)
            ctx["stale"] = self._stale_reason(row, ctx)
        rows.sort(key=lambda r: r["context"]["stale"] is not None)
        return rows if n is None else rows[:max(0, int(n))]

    def _ready_text(self, project=None, n=10, owner=None, agent=False):
        rows = self._ready_items(project=project, n=n, owner=owner, agent=agent)
        return ready_context_rows(rows, views.unblocks(self._all_items()))

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

    def _claim_guard(self, item, reason):
        """Refuse a stale claim with the facts, or return the override record."""
        reason = self._force_stale_reason(reason)
        index = {it["key"]: it for it in self._all_items()}
        ctx = self._task_context(item, index, fetch=True)
        facts = []
        commits = ctx["work_exists"]["commits"]
        if commits:
            head = commits[0]
            merge = head.get("merge") or {}
            facts.append(
                f"already merged: Plan-Item commit {head['sha'][:8]} ({head['date']})"
                + (f" via PR #{merge['pr']}" if merge.get("pr") else "")
                + " is on the default branch; attach evidence/submit instead")
        if ctx["newer_decisions"]:
            facts.append("premise outdated: " + ",".join(ctx["newer_decisions"])
                         + " names this item after its definition; re-confirm"
                         " (edit the item to cite it) first")
        if not facts:
            return None
        if reason is None:
            raise Refused("bad_state", f"{item['key']} stale: " + " | ".join(facts)
                          + " (override: force_stale=<reason>, audited)")
        return {"reason": reason, "overrode": facts}

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
