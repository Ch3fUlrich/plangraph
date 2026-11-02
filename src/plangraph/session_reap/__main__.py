"""__main__.py - CLI entrypoint for session reaper (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Sequence, Union

from .act import failure_tag, stop_session
from .log import log_stop, stopped_updated_at, DEFAULT_LOG_PATH
from .policy import decide, ReapConfig
from .scan import SessionRecord, read_session, scan


class ConfigError(ValueError):
    """The --config file is unusable. The run aborts instead of guessing defaults."""


_CONFIG_KEYS = ("done_grace_min", "idle_min", "allowlist")


def _positive_number(data: dict, key: str, default: float) -> float:
    if key not in data:
        return default
    val = data[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
        raise ConfigError(f"{key} must be a positive number, got {val!r}")
    return val


def load_config(path: Optional[Union[str, Path]]) -> ReapConfig:
    """Load and validate the optional config; any problem raises ConfigError.

    A silent fallback to defaults could reap earlier than the operator intended.
    `allowlist` holds extra patterns: they are added to DEFAULT_ALLOWLIST.
    """
    if not path:
        return ReapConfig()
    cfg_file = Path(path).expanduser()
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {cfg_file}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    unknown = sorted(set(data) - set(_CONFIG_KEYS))
    if unknown:
        raise ConfigError(f"unknown key(s): {', '.join(unknown)} (allowed: {', '.join(_CONFIG_KEYS)})")
    allowlist = data.get("allowlist", [])
    if not isinstance(allowlist, list) or not all(isinstance(p, str) and p.strip() for p in allowlist):
        raise ConfigError("allowlist must be a list of non-empty strings")
    return ReapConfig(
        done_grace_min=_positive_number(data, "done_grace_min", ReapConfig.done_grace_min),
        idle_min=_positive_number(data, "idle_min", ReapConfig.idle_min),
        allowlist=allowlist,
    )


def _age_min(updated_at: Optional[datetime], now: datetime) -> float:
    if updated_at is None:
        return 0.0
    return max(0.0, (_utc(now) - _utc(updated_at)).total_seconds() / 60.0)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _still_stoppable(rec: SessionRecord, now: datetime, cfg: ReapConfig) -> bool:
    """Re-read the session just before stopping it: it may have resumed since the scan."""
    fresh = read_session(rec.job_dir) if rec.job_dir else None
    return (
        fresh is not None
        and fresh.state == "done"
        and fresh.updated_at == rec.updated_at
        and decide(fresh, now, cfg)[0] == "stop"
    )


def reap_report(
    jobs_dir: Union[str, Path] = "~/.claude/jobs",
    config_path: Optional[Union[str, Path]] = None,
    apply: bool = False,
    runner: Optional[Callable[..., Any]] = None,
    log_path: Union[str, Path] = DEFAULT_LOG_PATH,
    now: Optional[datetime] = None,
) -> tuple[list[dict[str, Any]], int]:
    """Scan, decide, stop (only if apply) and log. Returns (rows, unreadable_count)."""
    current_time = _utc(now) if now else datetime.now(timezone.utc)
    cfg = load_config(config_path)
    scan_result = scan(jobs_dir)
    already_stopped = stopped_updated_at(log_path)

    results: list[dict[str, Any]] = []

    for rec in scan_result.records:
        action, reason = decide(rec, current_time, cfg)
        age_min = _age_min(rec.updated_at, current_time)
        row: dict[str, Any] = {
            "short_id": rec.short_id,
            "name": rec.name,
            "state": rec.state,
            "tempo": rec.tempo,
            "age_min": round(age_min, 1),
        }

        if action == "stop" and rec.updated_at is not None and already_stopped.get(rec.short_id) == rec.updated_at:
            # `claude stop` leaves state.json unchanged; the log is the only memory of the stop.
            action, reason = "keep", "already_stopped"

        if action == "stop" and apply:
            if not _still_stoppable(rec, current_time, cfg):
                action, reason = "keep", "changed_since_scan"
            else:
                rc, msg = stop_session(rec.short_id, apply=True, runner=runner)
                tag = failure_tag(rc, msg)
                log_stop(
                    short_id=rec.short_id,
                    name=rec.name,
                    age_min=age_min,
                    state=rec.state,
                    reason=f"{reason};stop_failed:{tag}" if tag else reason,
                    action=action,
                    rc=rc,
                    log_path=log_path,
                    updated_at=rec.updated_at,
                )
                row["rc"] = rc

        row["action"] = action
        row["reason"] = reason
        results.append(row)

    return results, scan_result.unreadable


def reap(**kwargs: Any) -> list[dict[str, Any]]:
    """reap_report() without the unreadable count."""
    return reap_report(**kwargs)[0]


def print_table(rows: list[dict[str, Any]], unreadable: int = 0) -> None:
    _print_rows(rows)
    if unreadable:
        print(f"{unreadable} unreadable state.json skipped (never stopped).")


def _print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No background sessions found.")
        return

    headers = ["SHORT_ID", "NAME", "STATE", "TEMPO", "AGE_MIN", "ACTION", "REASON"]
    col_widths = [len(h) for h in headers]

    str_rows: list[list[str]] = []
    for r in rows:
        row_vals = [
            str(r.get("short_id", "")),
            str(r.get("name", "")),
            str(r.get("state", "")),
            str(r.get("tempo", "")),
            str(r.get("age_min", "")),
            str(r.get("action", "")),
            str(r.get("reason", "")),
        ]
        str_rows.append(row_vals)
        for i, val in enumerate(row_vals):
            col_widths[i] = max(col_widths[i], len(val))

    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in col_widths))
    for row_vals in str_rows:
        print(fmt.format(*row_vals))


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runner: Optional[Callable[..., Any]] = None,
    log_path: Union[str, Path] = DEFAULT_LOG_PATH,
    now: Optional[datetime] = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="plangraph.session_reap",
        description="Reap idle or finished Claude Code background sessions.",
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=Path("~/.claude/jobs"),
        help="Path to Claude Code jobs directory (default: ~/.claude/jobs).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional config JSON file with grace/idle/allowlist settings.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply stops (default is dry-run report only).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output results as JSON.",
    )

    args = parser.parse_args(argv)

    try:
        results, unreadable = reap_report(
            jobs_dir=args.jobs_dir,
            config_path=args.config,
            apply=args.apply,
            runner=runner,
            log_path=log_path,
            now=now,
        )
    except ConfigError as exc:
        sys.stderr.write(f"session_reap: config error: {exc}\n")
        return 2

    if args.json:
        print(json.dumps({"sessions": results, "unreadable": unreadable}, indent=2))
    else:
        print_table(results, unreadable)

    return 0


if __name__ == "__main__":
    sys.exit(main())
