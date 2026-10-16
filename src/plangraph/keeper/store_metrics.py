"""Keeper metrics (schema v11): a daily ``(day, metric) -> value`` store plus the
governor-override audit verb.  All three verbs run through ``Store._run`` so each
is audited; the SQL is portable (``?`` placeholders, ``ON CONFLICT`` upsert) and
the writes sit inside ``Store._write()`` like ``store_cards._set_budget``.
"""
from __future__ import annotations

import math
import re
from datetime import date

from .errors import Refused

#: metric and provider names share the budget ``[A-Za-z0-9._:-]{1,64}`` alphabet.
_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_METRICS_MAX_ITEMS = 50
_METRICS_MAX_ROWS = 1000

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
