"""Read-only agreement report over the Jev shadow rows (RT-P1-JEV-SHADOW).

Jev answers the same routing / QC-accept questions as the rules, in shadow, and
its rows land in the keeper table ``jev_shadow``.  This module turns those raw
rows into the two public surfaces the show/API doors need: ``row_view`` for one
row and ``agreement_report`` for the per-kind summary.  It is deliberately pure
and stdlib-only -- no store, no network, no ``planner`` -- so a caller sees the
same numbers whichever door it used; only ``render`` formats them as text.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

__all__ = ["agrees", "row_view", "agreement_report", "render"]

# These mirror plangraph.keeper.schema.JEV_KINDS / JEV_STATUSES.  The schema
# names are added by a parallel writer, so we cannot import them yet; a test
# elsewhere asserts these tuples stay equal to the schema constants.
_KINDS = ("harness", "window", "budget", "review", "qc_accept")
_STATUSES = ("ok", "timeout", "unavailable", "error")

# Fields of the public row view, in display order.  ``key`` is internal routing
# bookkeeping and is dropped on purpose; ``agree`` is computed, not stored.
_VIEW_FIELDS = (
    "id", "attempt", "kind", "rule_answer", "jev_answer", "agree",
    "jev_probabilities", "jev_confidence", "status", "reason", "outcome",
    "jev_model", "latency_ms", "created_at", "outcome_at",
)

# The eight counters that ``agreement_report`` sums per kind and overall.
_COUNTERS = ("total", "ok", "agree", "disagree", "timeout", "unavailable",
             "error", "with_outcome")


def agrees(row: Mapping) -> bool | None:
    """Whether the rule and Jev answers agree, or None when Jev did not answer.

    Only a row whose ``status`` is ``"ok"`` has a comparable answer, and only
    when Jev actually produced one (``jev_answer`` is not None).  A timeout,
    unavailable or errored row is "no verdict", never a disagreement.
    """
    if row.get("status") != "ok":
        return None
    jev = row.get("jev_answer")
    if jev is None:
        return None
    return str(row.get("rule_answer")) == str(jev)


def row_view(row: Mapping) -> dict:
    """One stored ``jev_shadow`` row as the public show/API view.

    ``key`` (internal bookkeeping) is dropped.  ``jev_probabilities`` is stored
    as JSON text and is decoded here; bad, empty or non-dict text becomes None
    so callers never have to parse it themselves.  ``agree`` is computed from
    the row rather than read from storage.
    """
    view = {name: row.get(name) for name in _VIEW_FIELDS}
    view["agree"] = agrees(row)
    view["jev_probabilities"] = _decode_probabilities(row.get("jev_probabilities"))
    return view


def agreement_report(rows) -> dict:
    """Per-kind agreement statistics over an iterable of ``jev_shadow`` rows.

    Non-Mapping entries and rows whose ``kind`` is not one of ``_KINDS`` are
    skipped.  An unknown or missing ``status`` is counted as ``"error"``.  Every
    kind is present even with no rows.  ``agreement_rate`` is ``agree / ok``
    rounded to four places (None when there were no ok rows): timeout,
    unavailable and error rows are counted but excluded from the rate.  The
    ``overall`` entry sums the kinds before applying the same rate.
    """
    kinds = {kind: _blank_stats() for kind in _KINDS}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        kind = row.get("kind")
        if kind not in kinds:
            continue
        status = row.get("status")
        if status not in _STATUSES:
            status = "error"
        stats = kinds[kind]
        stats["total"] += 1
        if row.get("outcome") is not None:
            stats["with_outcome"] += 1
        if status == "ok":
            stats["ok"] += 1
            verdict = agrees(row)
            if verdict is True:
                stats["agree"] += 1
            elif verdict is False:
                stats["disagree"] += 1
        else:
            stats[status] += 1
    report = {"kinds": kinds, "overall": _sum_stats(kinds.values())}
    for stats in [*kinds.values(), report["overall"]]:
        _add_rate(stats)
    return report


def render(report: dict) -> str:
    """Compact deterministic text for one ``agreement_report`` result.

    One line per kind that has any rows, in ``_KINDS`` order, then an
    ``overall`` line; an all-empty report is the single line
    ``no jev shadow rows``.
    """
    kinds = report.get("kinds") or {}
    lines = [_render_line(kind, kinds[kind]) for kind in _KINDS
             if (kinds.get(kind) or {}).get("total")]
    overall = report.get("overall")
    if overall and overall.get("total"):
        lines.append(_render_line("overall", overall))
    return "\n".join(lines) if lines else "no jev shadow rows"


def _decode_probabilities(text):
    """The stored JSON-text probabilities as a dict, or None if unusable."""
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _blank_stats() -> dict:
    stats = {field: 0 for field in _COUNTERS}
    stats["agreement_rate"] = None
    return stats


def _sum_stats(all_stats) -> dict:
    total = _blank_stats()
    for stats in all_stats:
        for field in _COUNTERS:
            total[field] += stats[field]
    return total


def _add_rate(stats: dict) -> None:
    ok = stats["ok"]
    stats["agreement_rate"] = round(stats["agree"] / ok, 4) if ok else None


def _render_line(name: str, stats: dict) -> str:
    rate = stats.get("agreement_rate")
    rate_text = "-" if rate is None else f"{rate:.4f}"
    return (f"{name}  agree {stats['agree']}/{stats['ok']} (rate {rate_text})  "
            f"timeout {stats['timeout']}  unavailable {stats['unavailable']}  "
            f"error {stats['error']}  outcomes {stats['with_outcome']}")
