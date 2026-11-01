"""__main__.py - CLI entrypoint for session reaper (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Sequence, Union

from .act import stop_session
from .log import log_stop, DEFAULT_LOG_PATH
from .policy import decide, ReapConfig
from .scan import scan


def load_config(path: Optional[Union[str, Path]]) -> ReapConfig:
    if not path:
        return ReapConfig()
    cfg_file = Path(path).expanduser()
    if not cfg_file.is_file():
        return ReapConfig()
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return ReapConfig(
                done_grace_min=data.get("done_grace_min", ReapConfig.done_grace_min),
                idle_min=data.get("idle_min", ReapConfig.idle_min),
                allowlist=data.get("allowlist", ReapConfig.allowlist),
            )
    except Exception:
        pass
    return ReapConfig()


def reap(
    jobs_dir: Union[str, Path] = "~/.claude/jobs",
    config_path: Optional[Union[str, Path]] = None,
    apply: bool = False,
    runner: Optional[Callable[..., Any]] = None,
    log_path: Union[str, Path] = DEFAULT_LOG_PATH,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Scan sessions, make reap decisions, execute stops if apply=True, and log stops."""
    current_time = now or datetime.now(timezone.utc)
    cfg = load_config(config_path)
    scan_result = scan(jobs_dir)

    results: list[dict[str, Any]] = []

    for rec in scan_result.records:
        action, reason = decide(rec, current_time, cfg)

        age_min = 0.0
        if rec.updated_at is not None:
            now_dt = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            upd_dt = rec.updated_at if rec.updated_at.tzinfo else rec.updated_at.replace(tzinfo=timezone.utc)
            age_min = max(0.0, (now_dt - upd_dt).total_seconds() / 60.0)

        rc = 0
        if action == "stop" and apply:
            rc, _ = stop_session(rec.short_id, apply=True, runner=runner)
            log_stop(
                short_id=rec.short_id,
                name=rec.name,
                age_min=age_min,
                state=rec.state,
                reason=reason,
                action=action,
                rc=rc,
                log_path=log_path,
            )

        results.append({
            "short_id": rec.short_id,
            "name": rec.name,
            "state": rec.state,
            "tempo": rec.tempo,
            "age_min": round(age_min, 1),
            "action": action,
            "reason": reason,
        })

    return results


def print_table(rows: list[dict[str, Any]]) -> None:
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


def main(argv: Optional[Sequence[str]] = None) -> int:
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

    results = reap(
        jobs_dir=args.jobs_dir,
        config_path=args.config,
        apply=args.apply,
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
