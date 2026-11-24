"""Keeper metrics (schema v11): a daily ``(day, metric) -> value`` store plus the
governor-override audit verb.  All three verbs run through ``Store._run`` so each
is audited; the SQL is portable (``?`` placeholders, ``ON CONFLICT`` upsert) and
the writes sit inside ``Store._write()`` like ``store_cards._set_budget``.
"""
from __future__ import annotations

import math
import re
from datetime import date

from .. import views
from .errors import Refused

#: metric and provider names share the budget ``[A-Za-z0-9._:-]{1,64}`` alphabet.
_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_METRICS_MAX_ITEMS = 50
_METRICS_MAX_ROWS = 1000

#: Key/track rules of the category backfill, first match wins; no match leaves it to a model pass.
_CATEGORY_RULES = tuple((c, re.compile(p, re.I)) for c, p in (
    ("security", r"SECURITY|SECRET|CREDENTIAL|SCRUB|\bAUTH"),
    ("network", r"NETWORK|PROXY|\bDNS\b|FIREWALL|\bTLS\b|\bNET-"),
    ("telemetry", r"TELEMETRY|METRIC|COUNTER|DASHBOARD|\bALERT"),
    ("speed", r"SPEED|LATENCY|PERF|SLOW|THROUGHPUT|FASTER|\bFAST\b"),
    ("efficiency-tokens", r"TOKEN|COST|PROMPT-SIZE"),
    ("efficiency-quality", r"QUALITY|ACCURACY|PRECISION"),
    ("reliability", r"RELIAB|FLAKE|FLAKY|WATCHDOG|RETRY|RECOVER|STUCK|HANG|DEAD-SESSION"),
    ("bug-fix", r"\bBUG|DEFECT|REGRESSION|\bFIX\b|-FIX\b"),
    ("planning", r"\bPLAN|ROADMAP|DESIGN|RESEARCH"),
    ("ui-ux", r"\bUI\b|\bUX\b|WEB-|\bPAGE\b|TABLE-VIEW"),
    ("cleanup", r"CLEANUP|REMOVE|PRUNE|DEAD-CODE|ARCHIVE")))
_START_KINDS = frozenset(("planner", "expert", "judge", "answer", "worker_leg"))
_INITIATORS = frozenset(("operator", "agent"))


def _day(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError(value)
    except ValueError:
        raise Refused("validation", "day must be YYYY-MM-DD") from None
    return value


def _metric_name(value):
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise Refused("validation", "metric must match [A-Za-z0-9._:-]{1,64}")
    return value


def _metric_value(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise Refused("validation", "value must be a finite number")
    return float(value)


def _source(value):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 64:
        raise Refused("validation", "source must be a string of at most 64 characters")
    return value


class MetricsMixin:
    """The metrics verbs for :class:`plangraph.keeper.store.Store`."""

    def set_metrics(self, principal, day, items):
        return self._run(principal, "set_metrics", None,
                         lambda now: self._set_metrics(principal, day, items, now))

    def _set_metrics(self, principal, day, items, now):
        self._require_role(principal, "router", "system")
        _day(day)
        if not isinstance(items, list) or not 1 <= len(items) <= _METRICS_MAX_ITEMS:
            raise Refused("validation",
                          f"items must be a list of 1..{_METRICS_MAX_ITEMS} metrics")
        prepared = []
        for item in items:
            if not isinstance(item, dict):
                raise Refused("validation", "each metric must be an object")
            prepared.append((_metric_name(item.get("metric")),
                             _metric_value(item.get("value")),
                             _source(item.get("source"))))
        # validation above has already refused the bad ones; nothing is written
        # until every item has cleared, so a mixed batch leaves the table alone.
        stamp = self._iso(now)
        with self._write():
            for name, value, source in prepared:
                self.conn.execute(
                    "INSERT INTO metrics(day,metric,value,source,computed_at)"
                    " VALUES(?,?,?,?,?)"
                    " ON CONFLICT(day,metric) DO UPDATE SET value=excluded.value,"
                    "source=excluded.source,computed_at=excluded.computed_at",
                    (day, name, value, source, stamp))
        return {"day": day, "count": len(prepared)}

    def get_metrics(self, principal, day=None, since=None, metric=None):
        return self._run(principal, "get_metrics", None,
                         lambda now: self._get_metrics(day, since, metric))

    def _get_metrics(self, day, since, metric):
        clauses, params = [], []
        if day is not None:
            clauses.append("day=?")
            params.append(_day(day))
        if since is not None:
            clauses.append("day>=?")
            params.append(_day(since))
        if metric is not None:
            clauses.append("metric=?")
            params.append(_metric_name(metric))
        sql = ("SELECT day,metric,value,source,computed_at FROM metrics")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY day,metric LIMIT " + str(_METRICS_MAX_ROWS)
        rows = [{"day": r["day"], "metric": r["metric"], "value": r["value"],
                 "source": r["source"], "computed_at": r["computed_at"]}
                for r in self.conn.execute(sql, tuple(params))]
        return {"rows": rows}

    def log_governor_override(self, principal, start_kind, initiator, reason,
                              provider=None):
        return self._run(principal, "log_governor_override", None,
                         lambda now: self._log_governor_override(
                             principal, start_kind, initiator, reason, provider, now))

    def _log_governor_override(self, principal, start_kind, initiator, reason,
                               provider, now):
        self._require_role(principal, "router", "l1", "system", "spawner")
        if start_kind not in _START_KINDS:
            raise Refused("validation",
                          "start_kind must be one of planner, expert, judge, "
                          "answer, worker_leg")
        if initiator not in _INITIATORS:
            raise Refused("validation", "initiator must be operator or agent")
        if (not isinstance(reason, str) or not reason.strip()
                or len(reason) > 500):
            raise Refused("validation",
                          "reason must be a non-empty string of at most 500 characters")
        if provider is not None and (not isinstance(provider, str)
                                     or not _NAME_RE.fullmatch(provider)):
            raise Refused("validation", "provider must match [A-Za-z0-9._:-]{1,64}")
        # no new table, no event: the override is recorded as one audit row.
        msg = f"{start_kind}/{initiator}/{provider or '-'}: {reason}"[:500]
        self._audit(principal, "governor_override", None, True, "", msg, now)
        return {"logged": True}

    def _count_metrics(self, counts):
        """Add *counts* to today's counters (the near-copy and category experiments)."""
        now = self.now()
        with self._write():
            for name, n in counts.items():
                self.conn.execute(
                    "INSERT INTO metrics(day,metric,value,source,computed_at) VALUES(?,?,?,?,?)"
                    " ON CONFLICT(day,metric) DO UPDATE SET value=metrics.value+excluded.value,"
                    "computed_at=excluded.computed_at",
                    (self._iso(now)[:10], name, float(n), "count", self._iso(now)))

    # ------------------------------------------------ ITEM-CATEGORY-RANKED
    def _category_table(self):
        """``{project: [names in rank order]}`` for ``*`` and each project with its own rows: one query."""
        rows = {}
        for r in self.conn.execute("SELECT project, name, rank FROM categories ORDER BY rank, name"):
            rows.setdefault(r["project"], []).append(r["name"])
        return rows

    @staticmethod
    def _category_order(table, project):
        """Ranked names of *project*: its own order first, then every shared category it has
        not placed (a category added later is available everywhere)."""
        default, own = table.get("*", []), table.get(project, []) if project != "*" else []
        return own + [n for n in default if n not in own]

    def _category_list(self, project):
        return self._category_order(self._category_table(), project)

    def _rank_items(self, items, table=None):
        """Give each item ``category_rank``: its place in its project's list, none last."""
        table = self._category_table() if table is None else table
        ranks = {p: {n: i for i, n in enumerate(self._category_order(table, p))} for p in {*table, "*"}}
        for item in items:
            mine = ranks.get(item["project"]) or ranks["*"]
            item["category_rank"] = mine.get(item.get("category"), len(mine))
        return items

    def categories(self, principal, project=None):
        """The ranked category list of *project* and how many live items have none."""
        def read(now):
            names = self._category_list(project or "*")
            items = [i for i in self._all_items() if i["state"] not in ("done", "cancelled")
                     and (project is None or i["project"] == project)]
            return {"categories": [{"name": n, "rank": r} for r, n in enumerate(names)],
                    "uncategorised": sum(1 for i in items if not i.get("category"))}
        return self._run(principal, "categories", None, read)

    def add_category(self, principal, name, rank=None):
        """Append a category to the default list (router only; no code change)."""
        def add(now):
            self._require_role(principal, "router")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name):
                raise Refused("validation", "category must match [a-z][a-z0-9-]{1,31}")
            with self._write():
                if self.conn.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
                    raise Refused("conflict", f"category {name} exists")
                last = self.conn.execute(
                    "SELECT MAX(rank) FROM categories WHERE project='*'").fetchone()[0]
                self.conn.execute("INSERT INTO categories(project,name,rank) VALUES('*',?,?)",
                                  (name, (last if last is not None else -1) + 1))
            return {"added": name}
        return self._run(principal, "add_category", None, add)

    def rank_categories(self, principal, project, order):
        """Set *project*'s ranked list (the categories in *order* first, the rest after)."""
        def rank(now):
            self._require_role(principal, "router", "oversight")
            known = self._category_list("*")
            if not isinstance(project, str) or not project or project == "*":
                raise Refused("validation", "project must name a project")
            if (not isinstance(order, list) or len(set(order)) != len(order)
                    or any(n not in known for n in order)):
                raise Refused("validation", f"order must list distinct categories of {known}")
            names = list(order) + [n for n in self._category_list(project) if n not in order]
            with self._write():
                self.conn.execute("DELETE FROM categories WHERE project=?", (project,))
                for r, n in enumerate(names):
                    self.conn.execute("INSERT INTO categories(project,name,rank) VALUES(?,?,?)",
                                      (project, n, r))
            return {"project": project, "order": names}
        return self._run(principal, "rank_categories", None, rank)

    def backfill_categories(self, principal, project, dry_run=False):
        """Set the category of OPEN uncategorised items by key and track rules.

        Items in any other state are listed under ``skipped`` (an active item's
        definition is not the backfill's to edit); an edit that is refused is listed
        under ``failed``, so the result names what happened to every item; the
        ``unmatched`` keys are for a model pass with a sampled check."""
        def fill(now):
            self._require_role(principal, "router")
            chosen, unmatched, skipped, failed = {}, [], {}, {}
            for item in self._all_items():
                if (item["project"] != project or item["state"] in ("done", "cancelled")
                        or item.get("category") or item["kind"] in ("goal", "gate")):
                    continue
                if item["state"] != "open":
                    skipped[item["key"]] = item["state"]
                    continue
                text = f"{item['id']} {item.get('track') or ''}"
                match = next((c for c, rule in _CATEGORY_RULES if rule.search(text)), None)
                if match:
                    chosen[item["key"]] = match
                else:
                    unmatched.append(item["key"])
            if not dry_run:
                for key, name in list(chosen.items()):
                    try:
                        self._edit(principal, key, {"category": name}, now)
                    except Refused as exc:
                        failed[key] = f"{exc.code}: {exc.msg}"
                        del chosen[key]
            return {"set": chosen, "unmatched": unmatched, "skipped": skipped,
                    "failed": failed, "dry_run": bool(dry_run)}
        return self._run(principal, "backfill_categories", None, fill)

    def _record_categories(self, items, now):
        """EXPERIMENT (rule 3.76): per-day ready counts and verified merges per category
        (explicit zeros), and a finding for a category whose ready items waited over
        48 h with no verified merge.  An item is ready since its last dependency was
        done (its creation when it has none)."""
        stamp, day, cutoff = self._iso(now), self._iso(now)[:10], self._iso(now - 48 * 3600)
        created, done, _, merged_keys = self._snapshot_run_meta()
        cat = {i["key"]: i.get("category") for i in items}
        names = sorted({*self._category_list("*"), *(c for c in cat.values() if c)})
        ready, merged = {n: 0 for n in names}, {n: 0 for n in names}
        recent, waiting = set(), {}
        for item in views.ready(items):
            name = item.get("category")
            if not name:
                continue
            ready[name] += 1
            since = max([item.get("created_at") or created.get(item["key"]) or stamp]
                        + [done[d] for d in item.get("depends_on") or [] if d in done])
            if since <= cutoff and (name not in waiting or since < waiting[name][0]):
                waiting[name] = (since, item["key"])
        for key, ts in done.items():
            if cat.get(key) and key in merged_keys:       # a verified commit merge, not any closure
                if ts[:10] == day:
                    merged[cat[key]] += 1
                if ts >= cutoff:
                    recent.add(cat[key])
        for kind, counts in (("ready", ready), ("merged", merged)):
            for name, value in counts.items():
                self.conn.execute(
                    "INSERT INTO metrics(day,metric,value,source,computed_at) VALUES(?,?,?,?,?)"
                    " ON CONFLICT(day,metric) DO UPDATE SET value=excluded.value,"
                    "computed_at=excluded.computed_at",
                    (day, f"category_{kind}.{name}", float(value), "reap", stamp))
        starved = {key: name for name, (_, key) in waiting.items() if name not in recent}
        code = "category_starved"
        previous = {r["key"] for r in self.conn.execute(
            "SELECT key FROM alerts WHERE code=? AND state='open'", (code,))}
        for key, name in starved.items():
            self._alerts_raise_finding(
                key, code, f"category {name}: ready items waited over 48h with no verified merge", now)
        for key in sorted(previous - starved.keys()):
            self._alerts_resolve(key, now, code=code)
