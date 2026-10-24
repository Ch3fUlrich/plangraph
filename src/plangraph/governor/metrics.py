"""Pure computation of daily Claude usage metrics.

Ported from the reference usage sampler. All configuration is passed through
function arguments; no environment variables or home-relative paths are read.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os
import socket
import subprocess

__all__ = [
    "parse_ts",
    "iter_usage",
    "iter_usage_first",
    "weighted",
    "daily_weighted",
    "landed_by_day",
    "burn_index",
    "daily_metrics",
    "classify_session",
    "load_session_classes",
    "tag_records",
    "nearest_rank",
    "price_for",
    "read_multiplier",
    "record_cost",
    "ctx_report",
    "PRICE_TABLE",
    "DEFAULT_PRICE",
]

#: USD per million tokens ``(input, output, cache_write)``, matched against the
#: model id: the first entry whose key appears in the id wins (the keys are
#: disjoint). Cache reads are priced at ``read_multiplier(model)`` x the input
#: price of that entry.
PRICE_TABLE: dict[str, tuple[float, float, float]] = {
    "opus": (15.0, 75.0, 18.75),
    "sonnet": (3.0, 15.0, 3.75),
    "haiku": (1.0, 5.0, 1.25),
}
#: Documented default for a model no entry of :data:`PRICE_TABLE` names:
#: Sonnet-class pricing, ``$3`` input / ``$15`` output / ``$3.75`` cache write.
DEFAULT_PRICE: tuple[float, float, float] = (3.0, 15.0, 3.75)
#: Cache reads bill at this fraction of the input price for the 5.5 Claude
#: ids, and at :data:`READ_MULTIPLIER_OTHER` for every other model.
READ_MULTIPLIER_5_5 = 0.05
READ_MULTIPLIER_OTHER = 0.1
#: Per-call context above this many tokens counts towards ``share_over_200k``.
CONTEXT_OVER_200K = 200_000


def _as_int(v) -> int:
    """Best-effort int coercion used while reading token fields."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_ts(s) -> dt.datetime | None:
    """Parse an ISO timestamp into a UTC-aware datetime; ``Z`` and naive ok."""
    try:
        if not isinstance(s, str):
            return None
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None


def iter_usage(
    projects_dir,
    host: str | None = None,
    session_classes: dict[str, str] | None = None,
) -> list[dict]:
    """Collect per-assistant-message usage records from JSONL transcripts.

    Globs ``<projects_dir>/*/*.jsonl`` and ``<projects_dir>/*/*/subagents/*.jsonl``.
    Only lines with ``type == "assistant"`` and a ``message.usage`` dict count.
    Duplicates keyed on ``(message.id or uuid, requestId)`` are reduced to the
    record with the highest total tokens - the weighted/daily totals' rule;
    the ``ctx`` report counts the *first* record per message id instead (see
    :func:`iter_usage_first`). Malformed lines and files are skipped.

    Each record also carries ``host`` (``host``, else the local hostname) and
    ``session_class``: ``subagent`` for a transcript under a ``subagents/``
    directory or a record flagged ``isSidechain``, otherwise ``main`` refined
    by ``session_classes`` (see :func:`classify_session`), plus ``message_id``
    and ``request_id`` for dedup.
    """
    return _dedup_largest(_scan_usage(projects_dir, host, session_classes))


def iter_usage_first(
    projects_dir,
    host: str | None = None,
    session_classes: dict[str, str] | None = None,
) -> list[dict]:
    """The same records as :func:`iter_usage`, deduplicated the ctx/S6 way.

    The FIRST usage record per message id (``message.id``, else the line's
    ``uuid``) wins; records carrying no id at all are never merged. That is
    the reference scan's rule and the one the ``ctx`` report must follow,
    while :func:`iter_usage` keeps the *largest* record per ``(message id,
    requestId)`` for the weighted/daily totals - a separate pass over the
    transcripts keeps both rules (and both sets of expectations) intact.
    """
    return _dedup_first(_scan_usage(projects_dir, host, session_classes))


def _scan_usage(
    projects_dir,
    host: str | None = None,
    session_classes: dict[str, str] | None = None,
) -> list[dict]:
    """Parse usage records in timestamp, relative-file, line order, without dedup.

    Malformed lines and files, and missing or invalid timestamps, are skipped.
    Each record carries ``ts``,
    ``session``, ``model``, ``host``, ``session_class``, the four token
    counts, and the dedup keys ``message_id`` (``message.id``, else the
    line's ``uuid``) and ``request_id``.
    """
    host_name = host or socket.gethostname()
    patterns = [
        os.path.join(projects_dir, "*", "*.jsonl"),
        os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"),
    ]
    records: list[dict] = []
    for pattern in patterns:
        for path in sorted(glob.iglob(pattern)):
            rel = os.path.relpath(path, projects_dir).replace(os.sep, "/")
            parts = rel.split("/")
            subagent = "/subagents/" in rel
            if subagent:
                session = parts[1] if len(parts) > 1 else os.path.splitext(os.path.basename(path))[0]
            else:
                session = os.path.splitext(parts[1])[0] if len(parts) > 1 else os.path.splitext(os.path.basename(path))[0]
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line_number, line in enumerate(f, 1):
                        if '"usage"' not in line or '"assistant"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(d, dict):
                            continue
                        if d.get("type") != "assistant":
                            continue
                        msg = d.get("message")
                        if not isinstance(msg, dict):
                            msg = {}
                        usage = msg.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        ts = parse_ts(d.get("timestamp") or msg.get("timestamp"))
                        if ts is None:
                            # Like daily_weighted, undated usage cannot enter a day.
                            continue
                        input_tokens = _as_int(usage.get("input_tokens"))
                        output_tokens = _as_int(usage.get("output_tokens"))
                        cache_read = _as_int(usage.get("cache_read_input_tokens"))
                        cache_write = _as_int(usage.get("cache_creation_input_tokens"))
                        model = d.get("model") or msg.get("model") or ""
                        sidechain = bool(d.get("isSidechain") or msg.get("isSidechain"))
                        base_class = "subagent" if (subagent or sidechain) else "main"
                        message_id = msg.get("id") or d.get("uuid")
                        request_id = d.get("requestId")
                        if any(value is not None and not isinstance(value, (str, int))
                               for value in (message_id, request_id)):
                            continue
                        records.append({
                            "file": rel,
                            "line": line_number,
                            "ts": ts,
                            "session": session,
                            "model": model,
                            "host": host_name,
                            "session_class": classify_session(
                                session, base_class, session_classes),
                            "message_id": message_id,
                            "request_id": request_id,
                            "input": input_tokens,
                            "output": output_tokens,
                            "cache_read": cache_read,
                            "cache_write": cache_write,
                        })
            except Exception:
                continue
    # Directory enumeration order must not choose the ctx report's first call.
    return sorted(records, key=lambda rec: (rec["ts"], rec["file"], rec["line"]))


def _dedup_largest(records: list[dict]) -> list[dict]:
    """Keep the highest-token record per ``(message id, requestId)``.

    First record wins a tie; missing ids remain part of the legacy pair key.
    """
    best: dict[tuple, dict] = {}
    totals: dict[tuple, int] = {}
    for rec in records:
        key = (rec.get("message_id"), rec.get("request_id"))
        total = rec["input"] + rec["output"] + rec["cache_read"] + rec["cache_write"]
        if key not in best or total > totals[key]:
            best[key] = rec
            totals[key] = total
    return list(best.values())


def _dedup_first(records: list[dict]) -> list[dict]:
    """Keep the first record per message id; records without an id all stay."""
    seen: set = set()
    kept: list[dict] = []
    for rec in records:
        key = rec.get("message_id")
        if not key:
            kept.append(rec)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(rec)
    return kept


def weighted(rec: dict) -> float:
    """Weighted token cost: cache reads count as one tenth."""
    return float(rec["input"] + rec["output"] + rec["cache_write"] + 0.1 * rec["cache_read"])


def classify_session(
    session: str | None,
    session_class: str = "main",
    session_classes: dict[str, str] | None = None,
) -> str:
    """Refine ``session_class`` from a launch-record mapping.

    A ``subagent`` transcript (``subagents/`` path or ``isSidechain``) is never
    renamed. A ``main`` record is refined to whatever a launch record names for
    ``session`` - ``router``, ``L1``, ``judge``, ``worker``, ... - and a
    session no launch record names stays ``main``. Session names themselves are
    never interpreted.
    """
    if session_class == "subagent":
        return "subagent"
    if session_class == "main" and session_classes:
        named = session_classes.get(session)
        if isinstance(named, str) and named.strip():
            return named.strip()
    return session_class


def load_session_classes(path) -> dict[str, str]:
    """Read a ``{session_id: class}`` launch-record mapping file (JSON)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"session-classes file must hold a JSON object: {path}")
    return {str(key): value for key, value in data.items()
            if isinstance(value, str) and value.strip()}


def tag_records(
    records: list[dict],
    host: str | None = None,
    session_classes: dict[str, str] | None = None,
) -> list[dict]:
    """Stamp ``host`` and refine ``session_class`` on already-parsed records.

    Idempotent: a record already refined by :func:`classify_session` keeps its
    class when the mapping is re-applied. Returns ``records`` itself.
    """
    for rec in records:
        if host:
            rec["host"] = host
        rec["session_class"] = classify_session(
            rec.get("session"), rec.get("session_class", "main"), session_classes)
    return records


def daily_weighted(records: list[dict]) -> dict[str, float]:
    """Sum weighted tokens grouped by UTC calendar day ``YYYY-MM-DD``."""
    days: dict[str, float] = {}
    for rec in records:
        ts = rec.get("ts")
        if not isinstance(ts, dt.datetime):
            continue
        day = ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
        days[day] = days.get(day, 0.0) + weighted(rec)
    return days


def _default_runner(argv: list[str]) -> str:
    """Run a git command and return stdout; raise on non-zero exit."""
    return subprocess.run(argv, check=True, text=True, capture_output=True).stdout


def landed_by_day(
    repo_dir,
    since: str,
    until: str | None = None,
    ref: str = "HEAD",
    runner=None,
) -> dict[str, int]:
    """Count first-parent merge commits per UTC day from a git log.

    ``since``/``until`` are ISO date strings. ``runner`` is injectable for
    tests and takes the argv list, returning stdout. Empty dict on git failure.
    """
    if runner is None:
        runner = _default_runner
    argv = [
        "git",
        "-C",
        str(repo_dir),
        "log",
        "--first-parent",
        "--merges",
        "--since=" + str(since),
    ]
    if until is not None:
        argv.append("--until=" + str(until))
    argv += ["--format=%cI", str(ref)]
    try:
        out = runner(argv)
    except (OSError, subprocess.CalledProcessError):
        return {}
    counts: dict[str, int] = {}
    for line in out.splitlines():
        ts = parse_ts(line.strip())
        if ts is None:
            continue
        day = ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
        counts[day] = counts.get(day, 0) + 1
    return counts


def burn_index(day_weighted: float, weekly_cap: float | None) -> float | None:
    """Burn ratio versus an even weekly allowance; None when cap is unusable."""
    if weekly_cap is None or weekly_cap <= 0:
        return None
    return day_weighted / (weekly_cap / 7)


def daily_metrics(
    day: str,
    records: list[dict],
    landed: dict[str, int],
    weekly_cap: float | None,
) -> list[dict]:
    """Build the metric rows for one UTC day."""
    day_total = daily_weighted(records).get(day, 0.0)
    landed_count = int(landed.get(day, 0))

    rows: list[dict] = [
        {"metric": "claude_weighted_tokens", "value": round(float(day_total), 3), "source": "transcripts"},
    ]
    burn = burn_index(day_total, weekly_cap)
    if burn is not None:
        rows.append({"metric": "claude_burn_index", "value": round(burn, 3), "source": "transcripts"})
    rows.append({"metric": "landed_changes", "value": round(float(landed_count), 3), "source": "git"})
    if landed_count > 0:
        rows.append(
            {
                "metric": "weighted_tokens_per_landed",
                "value": round(day_total / landed_count, 3),
                "source": "derived",
            }
        )
    return rows


def price_for(model: str | None) -> tuple[float, float, float]:
    """``(input, output, cache_write)`` USD per million tokens for ``model``."""
    text = (model or "").lower()
    for name, price in PRICE_TABLE.items():
        if name in text:
            return price
    return DEFAULT_PRICE


def read_multiplier(model: str | None) -> float:
    """Cache-read discount versus the input price: 0.05x on the 5.5 ids."""
    text = (model or "").lower()
    if "opus-5-5" in text or "sonnet-5-5" in text:
        return READ_MULTIPLIER_5_5
    return READ_MULTIPLIER_OTHER


def record_cost(rec: dict) -> float:
    """USD for one call: cache reads at ``read_multiplier x input price``."""
    model = rec.get("model")
    input_price, output_price, write_price = price_for(model)
    return (
        _as_int(rec.get("input")) * input_price
        + _as_int(rec.get("output")) * output_price
        + _as_int(rec.get("cache_write")) * write_price
        + _as_int(rec.get("cache_read")) * read_multiplier(model) * input_price
    ) / 1_000_000


def nearest_rank(values, p: float):
    """Nearest-rank percentile: the value at 1-based rank ``ceil(p * n)``.

    Returns ``0`` for an empty sample.
    """
    if not values:
        return 0
    ordered = sorted(values)
    index = max(math.ceil(p * len(ordered)) - 1, 0)
    return ordered[index]


def ctx_report(records: list[dict], day: str | None = None) -> dict:
    """Per ``(host, session_class)`` context/cache report for one UTC day.

    Dedup first: the FIRST usage record per message id (``message_id``, i.e.
    ``message.id`` or the transcript line's ``uuid``) counts, later duplicates
    of that id are ignored, and a record carrying no id is never merged with
    another. That is the S6 reference rule, and it deliberately differs from
    :func:`iter_usage`, which keeps the *largest* record per ``(message id,
    requestId)`` for the weighted/daily totals - feed this report the
    :func:`iter_usage_first` records (or any list still holding duplicates)
    so the first one survives.

    ``day`` is ``YYYY-MM-DD`` (``None`` reports every record). Each group holds:

    - ``calls`` and ``weighted`` (:func:`weighted`, cache reads at one tenth);
    - ``share_cache_read`` / ``share_cache_write`` / ``share_output``: each
      component as a share of the *raw* token total
      (``input + output + cache_read + cache_write``);
    - ``input_from_cache``: ``cache_read / (input + cache_read + cache_write)``;
    - ``ctx_p50`` / ``ctx_p90`` / ``ctx_p99``: nearest-rank percentiles of the
      per-call context ``input + cache_read + cache_write``;
    - ``share_over_200k``: fraction of calls whose context exceeds
      :data:`CONTEXT_OVER_200K`;
    - ``cost_usd``: :func:`record_cost` summed, i.e. the price table with cache
      reads at :func:`read_multiplier` x the input price.
    """
    records = _dedup_first(records)
    groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        if day is not None:
            ts = rec.get("ts")
            if not isinstance(ts, dt.datetime):
                continue
            if ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d") != day:
                continue
        key = (str(rec.get("host") or ""), str(rec.get("session_class") or "main"))
        groups.setdefault(key, []).append(rec)

    rows: list[dict] = []
    for (host, session_class) in sorted(groups):
        members = groups[(host, session_class)]
        calls = len(members)
        raw = {
            "input": sum(_as_int(m.get("input")) for m in members),
            "output": sum(_as_int(m.get("output")) for m in members),
            "cache_read": sum(_as_int(m.get("cache_read")) for m in members),
            "cache_write": sum(_as_int(m.get("cache_write")) for m in members),
        }
        raw_total = sum(raw.values())
        cache_total = raw["input"] + raw["cache_read"] + raw["cache_write"]
        context = [m["input"] + m["cache_read"] + m["cache_write"] for m in members]
        rows.append({
            "host": host,
            "session_class": session_class,
            "calls": calls,
            "weighted": round(sum(weighted(m) for m in members), 3),
            "share_cache_read": round(raw["cache_read"] / raw_total, 6) if raw_total else 0.0,
            "share_cache_write": round(raw["cache_write"] / raw_total, 6) if raw_total else 0.0,
            "share_output": round(raw["output"] / raw_total, 6) if raw_total else 0.0,
            "input_from_cache": round(raw["cache_read"] / cache_total, 6) if cache_total else 0.0,
            "ctx_p50": nearest_rank(context, 0.5),
            "ctx_p90": nearest_rank(context, 0.9),
            "ctx_p99": nearest_rank(context, 0.99),
            "share_over_200k": round(
                sum(1 for c in context if c > CONTEXT_OVER_200K) / calls, 6),
            "cost_usd": round(sum(record_cost(m) for m in members), 6),
        })
    return {"day": day, "groups": rows}
