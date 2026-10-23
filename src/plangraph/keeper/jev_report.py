"""Read-only agreement report over the Jev shadow rows (RT-P1-JEV-SHADOW).

Jev answers the same routing questions as the rules, in shadow, and
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
# names are added by a parallel writer, so we cannot import them yet; the test
# ``test_report_kind_and_status_tuples_match_the_schema`` asserts these tuples
# stay equal to the schema constants.
_KINDS = ("harness", "window", "budget", "review", "qc_accept")
_STATUSES = ("ok", "timeout", "unavailable", "error")

#: The reason ``jev_client`` stamps on an auto-mode answer it produced WITHOUT a
#: key: a placeholder, never Jev's opinion.  Such rows are counted but excluded
#: from ``agreement_rate``/``kappa`` and reported as ``stub_count`` instead.
#: Copied (not imported) so this module keeps its no-planner promise; the test
#: ``test_stub_marker_matches_the_client_auto_fallback`` keeps it in step with
#: ``plangraph.planner.jev_client``.
_STUB_REASON = "stub (no key)"

# Fields of the public row view, in display order.  ``key`` is internal routing
# bookkeeping and is dropped on purpose; ``agree`` is computed, not stored.
_VIEW_FIELDS = (
    "id", "attempt", "decision_id", "input_hash", "kind", "rule_answer",
    "jev_answer", "agree", "jev_probabilities", "jev_confidence", "status",
    "reason", "outcome", "jev_model", "latency_ms", "created_at",
    "outcome_at",
)

# The ten counters that ``agreement_report`` sums per kind and overall.
_COUNTERS = ("total", "ok", "agree", "disagree", "timeout", "unavailable",
             "error", "with_outcome", "no_answer", "stub_count")


def agrees(row: Mapping) -> bool | None:
    """Whether the rule and Jev answers agree, or None when Jev did not answer.

    Only a row whose ``status`` is ``"ok"`` has a comparable answer, and only
    when Jev actually produced one (``jev_answer`` is not None).  A timeout,
    unavailable or errored row is "no verdict", never a disagreement; the same
    goes for an auto-mode stub row, which is a placeholder from a client with no
    key rather than an answer from Jev.
    """
    if row.get("status") != "ok" or _is_stub(row):
        return None
    jev = row.get("jev_answer")
    if jev is None:
        return None
    return str(row.get("rule_answer")) == str(jev)


def _is_stub(row: Mapping) -> bool:
    """True for an auto-mode placeholder answer (Jev was never actually asked).

    Such a row still records what the rules answered and that no model was
    available, so it is counted - but it carries no Jev opinion and therefore
    never enters ``agreement_rate`` or ``kappa`` (see ``stub_count``).
    """
    reason = row.get("reason")
    return isinstance(reason, str) and reason == _STUB_REASON


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
    unavailable and error rows are counted but excluded from the rate, and so
    are auto-mode stub rows - a placeholder answer from a client with no key is
    no Jev opinion - which are counted on their own ``stub_count`` axis instead.
    Each axis also reports ``kappa`` (Cohen's kappa, None when undefined),
    ``confusion`` (nested dict in sorted label order), ``labels`` (sorted list)
    and ``no_answer`` (count of non-usable rows).  The ``overall`` entry sums
    the kinds and applies the same metrics across all rows.
    """
    kinds = {kind: _blank_stats() for kind in _KINDS}
    kind_pairs: dict[str, list[tuple[str, str]]] = {kind: [] for kind in _KINDS}
    all_pairs: list[tuple[str, str]] = []

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
        if _is_stub(row):
            # A placeholder answer (auto mode, no key): counted, never scored.
            # It never enters ``ok``, the agreement rate or the kappa pairs.
            stats["stub_count"] += 1
            continue
        if status == "ok":
            stats["ok"] += 1
            verdict = agrees(row)
            if verdict is True:
                stats["agree"] += 1
            elif verdict is False:
                stats["disagree"] += 1

            jev = row.get("jev_answer")
            rule = row.get("rule_answer")
            if jev is not None and rule is not None:
                pair = (str(rule), str(jev))
                kind_pairs[kind].append(pair)
                all_pairs.append(pair)
            else:
                stats["no_answer"] += 1
        else:
            stats[status] += 1
            stats["no_answer"] += 1

    overall = _sum_stats(kinds.values())
    report = {"kinds": kinds, "overall": overall}

    for kind in _KINDS:
        stats = kinds[kind]
        _add_rate(stats)
        kappa, confusion, labels = _compute_kappa_and_confusion(kind_pairs[kind])
        stats["kappa"] = kappa
        stats["confusion"] = confusion
        stats["labels"] = labels

    _add_rate(overall)
    overall_kappa, overall_confusion, overall_labels = _compute_kappa_and_confusion(all_pairs)
    overall["kappa"] = overall_kappa
    overall["confusion"] = overall_confusion
    overall["labels"] = overall_labels

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
    stats["kappa"] = None
    stats["confusion"] = {}
    stats["labels"] = []
    return stats


def _compute_kappa_and_confusion(
    pairs: list[tuple[str, str]],
) -> tuple[float | None, dict[str, dict[str, int]], list[str]]:
    """Compute Cohen's kappa, sorted confusion matrix, and sorted labels.

    Uses integer arithmetic for numerator and denominator to prevent float
    rounding inaccuracies, rounding kappa to 4 decimal places at the end.
    Returns (None, {}, []) when there are no usable pairs.
    """
    if not pairs:
        return None, {}, []

    label_set: set[str] = set()
    pair_counts: dict[tuple[str, str], int] = {}
    for r, j in pairs:
        label_set.add(r)
        label_set.add(j)
        pair_counts[(r, j)] = pair_counts.get((r, j), 0) + 1

    labels = sorted(label_set)
    confusion = {
        r: {j: pair_counts.get((r, j), 0) for j in labels}
        for r in labels
    }

    n = len(pairs)
    r_counts = {lbl: sum(confusion[lbl][j] for j in labels) for lbl in labels}
    j_counts = {lbl: sum(confusion[r][lbl] for r in labels) for lbl in labels}
    agree_count = sum(confusion[lbl][lbl] for lbl in labels)

    sum_pe = sum(r_counts[lbl] * j_counts[lbl] for lbl in labels)
    denom = n * n - sum_pe
    if denom == 0:
        kappa = None
    else:
        num = n * agree_count - sum_pe
        val = round(num / denom, 4)
        kappa = 0.0 if val == 0.0 else val

    return kappa, confusion, labels


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
            f"error {stats['error']}  outcomes {stats['with_outcome']}  "
            f"stubs {stats.get('stub_count', 0)}")
