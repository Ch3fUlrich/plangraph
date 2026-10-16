"""Pure computation of daily Claude usage metrics.

Ported from the reference usage sampler. All configuration is passed through
function arguments; no environment variables or home-relative paths are read.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import subprocess

__all__ = [
    "parse_ts",
    "iter_usage",
    "weighted",
    "daily_weighted",
    "landed_by_day",
    "burn_index",
    "daily_metrics",
]


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


def iter_usage(projects_dir) -> list[dict]:
    """Collect per-assistant-message usage records from JSONL transcripts.

    Globs ``<projects_dir>/*/*.jsonl`` and ``<projects_dir>/*/*/subagents/*.jsonl``.
    Only lines with ``type == "assistant"`` and a ``message.usage`` dict count.
    Duplicates keyed on ``(message.id or uuid, requestId)`` are reduced to the
    record with the highest total tokens. Malformed lines and files are skipped.
    """
    patterns = [
        os.path.join(projects_dir, "*", "*.jsonl"),
        os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"),
    ]
    best: dict[tuple, dict] = {}
    totals: dict[tuple, int] = {}
    for pattern in patterns:
        for path in glob.iglob(pattern):
            rel = os.path.relpath(path, projects_dir).replace(os.sep, "/")
            parts = rel.split("/")
            subagent = "/subagents/" in rel
            if subagent:
                session = parts[1] if len(parts) > 1 else os.path.splitext(os.path.basename(path))[0]
            else:
                session = os.path.splitext(parts[1])[0] if len(parts) > 1 else os.path.splitext(os.path.basename(path))[0]
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
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
                            continue
                        input_tokens = _as_int(usage.get("input_tokens"))
                        output_tokens = _as_int(usage.get("output_tokens"))
                        cache_read = _as_int(usage.get("cache_read_input_tokens"))
                        cache_write = _as_int(usage.get("cache_creation_input_tokens"))
                        total = input_tokens + output_tokens + cache_read + cache_write
                        key = (msg.get("id") or d.get("uuid"), d.get("requestId"))
                        model = d.get("model") or msg.get("model") or ""
                        rec = {
                            "ts": ts,
                            "session": session,
                            "model": model,
                            "input": input_tokens,
                            "output": output_tokens,
                            "cache_read": cache_read,
                            "cache_write": cache_write,
                        }
                        if key not in best or total > totals[key]:
                            best[key] = rec
                            totals[key] = total
            except Exception:
                continue
    return list(best.values())


def weighted(rec: dict) -> float:
    """Weighted token cost: cache reads count as one tenth."""
    return float(rec["input"] + rec["output"] + rec["cache_write"] + 0.1 * rec["cache_read"])


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
