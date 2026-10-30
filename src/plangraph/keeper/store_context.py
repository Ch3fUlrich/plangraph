"""Full task context on reads, stale-claim refusal on ``claim`` (TASK-CONTEXT-GUARD, D-3215).

Sessions started work that was already merged, cancelled or overtaken by a
later decision because ``ready``/``show``/``claim`` handed them a thin record.
This mixin adds, per item:

* ``dep_states`` / ``dependents``: every dependency and dependent with its
  state and, when terminal or replaced, why (cancel reason, ``corrected by``);
* ``linked_decisions``: D-ids cited in the item text or naming its key in the
  decisions log; ``superseded_premises`` maps a cited id to the row that
  superseded it; ``newer_decisions`` lists rows naming the key that are newer
  than the item's premise (see :meth:`_watermark`) and not cited by it;
* ``last_activity``: the newest event (time, actor, type);
* ``work_exists``: ``Plan-Item`` commits on the default ref and open lanes
  (``None`` = unknown, e.g. no repo configured);
* ``stale``: ``None`` or the joined reasons (untouched > 7 days, superseded
  premise, newer decision, already merged).

``claim`` refuses an item with a merged ``Plan-Item`` commit or one a newer
decision names, unless ``force_stale`` carries a reason (a cancelled item or
dependency is refused as before, now with its cancel reason); the override is recorded on the ``claimed`` event.  Unknown facts
(no repo, git failure, no decisions file) never refuse.
"""
from __future__ import annotations

import json

from .. import views
from . import context_sources as src
from .brief import ensure_cap
from .errors import Refused
from .fmt import ready_context_rows

__all__ = ["ContextMixin", "STALE_DAYS", "READY_GIT_LIMIT"]

STALE_DAYS = 7
#: ``ready`` runs the git check on at most this many rows.
READY_GIT_LIMIT = 25
_DEF_TYPES = ("item_created", "item_defined")
_CLOCK_WINDOW = 300
_FORCE_MAX = 500


def _payload(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _num(did):
    return int(did.split("-", 1)[1])


class ContextMixin:
    """Task-context reads and the stale-claim guard."""

    # ------------------------------------------------------------ inputs
    def _decision_inputs(self):
        rows = src.decision_rows(self._adapter("decisions"))
        return rows, src.superseded_map(rows)

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
        out = {}
        for key in keys:
            row = self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='cancelled'"
                " ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
            if row:
                out[key] = _payload(row["payload"]).get("reason") or "cancelled"
        return out

    def _watermark(self, item):
        """The newest decision the item's premise knew of, as a number.

        The highest D-id cited in the item text; failing that, the "decision
        clock" at its last definition: the highest D-id cited by the last
        definitions recorded up to then.  ``None`` = unknown (never refuses).
        """
        cited = src.decision_ids(*self._texts(item))
        if cited:
            return _num(cited[-1])
        last = self.conn.execute(
            "SELECT MAX(seq) AS m FROM events WHERE key=? AND type IN (?,?)",
            (item["key"], *_DEF_TYPES)).fetchone()
        if not last or last["m"] is None:
            return None
        rows = self.conn.execute(
            "SELECT payload FROM events WHERE type IN (?,?) AND seq<=?"
            " ORDER BY seq DESC LIMIT ?",
            (*_DEF_TYPES, last["m"], _CLOCK_WINDOW)).fetchall()
        ids = src.decision_ids(*(r["payload"] if isinstance(r["payload"], str)
                                 else json.dumps(r["payload"]) for r in rows))
        return _num(ids[-1]) if ids else None

    @staticmethod
    def _texts(item):
        return [item.get(name) for name in
                ("title", "description", "done_criteria", "note")]

    # ----------------------------------------------------------- context
    def _task_context(self, item, index, *, inputs=None, activity=None,
                      git=True, fetch=False):
        key = item["key"]
        rows, superseded = inputs or self._decision_inputs()
        dependents = sorted(o["key"] for o in index.values()
                            if key in (o.get("depends_on") or []))
        deps = list(item.get("depends_on") or [])
        reasons = self._reasons([k for k in deps + dependents
                                 if index.get(k, {}).get("state") == "cancelled"])
        corrected = {}
        for other in index.values():
            if other.get("corrects"):
                corrected.setdefault(other["corrects"], []).append(other["key"])

        def entry(k):
            why = reasons.get(k)
            if corrected.get(k):
                why = (why + "; " if why else "") + "corrected by " + \
                    ",".join(sorted(corrected[k]))
            return {"key": k, "state": index.get(k, {}).get("state"), "reason": why}

        cited = src.decision_ids(*self._texts(item))
        naming = src.decisions_naming(rows, key)
        mark = self._watermark(item)
        newer = [d for d in naming if d not in cited
                 and mark is not None and _num(d) > mark]
        last = (activity.get(key) if activity is not None
                else self._last_activity(key))
        work = {"commits": None, "lanes": None}
        if git:
            gitad = self._adapter("git")
            work = {"commits": src.plan_item_commits(gitad, item["project"], key,
                                                     fetch=fetch),
                    "lanes": src.open_lanes(gitad, item["project"], key)}
        ctx = {
            "dep_states": [entry(k) for k in deps],
            "dependents": [entry(k) for k in dependents],
            "corrected_by": sorted(corrected.get(key, [])),
            "linked_decisions": sorted(set(cited) | set(naming), key=_num),
            "superseded_premises": {d: superseded[d] for d in cited if d in superseded},
            "newer_decisions": newer,
            "last_activity": last,
            "work_exists": work,
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
        ctx = self._task_context(item, index)
        commits = ctx["work_exists"]["commits"]
        if commits:
            brief["merged"] = commits[0]["sha"][:8] + (
                f" PR#{commits[0]['merge']['pr']}"
                if (commits[0].get("merge") or {}).get("pr") else "")
        if ctx["linked_decisions"]:
            brief["decisions"] = ",".join(ctx["linked_decisions"])
        if ctx["stale"]:
            brief["stale"] = ctx["stale"]
        return ensure_cap(brief)

    # -------------------------------------------------------------- ready
    def _ready_items(self, project=None, n=10, owner=None, agent=False):
        """``views.ready`` rows with ``context``; stale rows sort last."""
        items = self._all_items()
        index = {it["key"]: it for it in items}
        rows = views.ready(items, project=project, owner=owner, agent=agent)
        inputs, activity = self._decision_inputs(), self._last_activity_map()
        for row in rows:
            row["context"] = self._task_context(row, index, inputs=inputs,
                                                activity=activity, git=False)
        rows.sort(key=lambda r: r["context"]["stale"] is not None)  # stable
        if n is not None:
            rows = rows[:max(0, int(n))]
        for row in rows[:READY_GIT_LIMIT]:
            ctx = row["context"]
            gitad = self._adapter("git")
            ctx["work_exists"] = {
                "commits": src.plan_item_commits(gitad, row["project"], row["key"],
                                                 fetch=False),
                "lanes": src.open_lanes(gitad, row["project"], row["key"])}
            ctx["stale"] = self._stale_reason(row, ctx)
        rows.sort(key=lambda r: r["context"]["stale"] is not None)
        return rows

    def _ready_text(self, project=None, n=10, owner=None, agent=False):
        rows = self._ready_items(project=project, n=n, owner=owner, agent=agent)
        return ready_context_rows(rows, views.unblocks(self._all_items()))

    # -------------------------------------------------------------- claim
    def _claim_guard(self, item, force_stale):
        """Refuse a stale claim with the facts, or return the override record."""
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
        reason = force_stale.strip() if isinstance(force_stale, str) else ""
        if not reason:
            raise Refused("bad_state", f"{item['key']} stale: " + " | ".join(facts)
                          + " (override: force_stale=<reason>, audited)")
        return {"reason": reason[:_FORCE_MAX], "overrode": facts}

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
