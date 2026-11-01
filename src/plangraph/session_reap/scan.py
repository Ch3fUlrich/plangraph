"""scan.py - scan ~/.claude/jobs/*/state.json (SESSION-AUTO-CLOSE, D-2442).

Everything here fails closed: a value that cannot be read exactly as documented
makes the session look *less* stoppable, never more.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union


def parse_iso_dt(val: Any) -> Optional[datetime]:
    """Parse an ISO timestamp into an aware UTC datetime.

    Strings without an explicit offset are rejected (None): a naive timestamp
    from a local-time writer can look hours older than it is, and an older
    timestamp only ever makes a session easier to stop.
    """
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    if not isinstance(val, str) or not val.strip():
        return None
    s = val.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_ttl_marker(raw: Any) -> tuple[Optional[bool], Optional[float], Optional[str]]:
    """Strictly parse a ttl.json body into (allow_stop, ttl_min, error).

    allow_stop accepts only JSON true/false or the integers 0/1; ttl_min only a
    finite JSON number. Anything else returns an error and allow_stop=False, so
    a garbled operator marker keeps the session instead of releasing it.
    """
    if not isinstance(raw, dict):
        return False, None, "ttl.json is not an object"
    allow_stop: Optional[bool] = None
    ttl_min: Optional[float] = None
    if "allow_stop" in raw:
        v = raw["allow_stop"]
        if isinstance(v, bool):
            allow_stop = v
        elif isinstance(v, int) and v in (0, 1):
            allow_stop = bool(v)
        else:
            return False, None, "allow_stop is not true/false"
    if "ttl_min" in raw:
        v = raw["ttl_min"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return False, None, "ttl_min is not a finite number"
        ttl_min = v
    return allow_stop, ttl_min, None


@dataclass
class SessionRecord:
    """Session state record extracted from state.json."""

    short_id: str
    name: str
    state: str
    tempo: str
    updated_at: Optional[datetime]
    has_result: bool
    job_dir: Optional[Path] = None
    ttl: Optional[dict] = None
    allow_stop: Optional[bool] = None
    ttl_min: Optional[float] = None
    ttl_error: Optional[str] = None

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class ScanResult(NamedTuple):
    records: list[SessionRecord]
    unreadable: int

    @property
    def unreadable_count(self) -> int:
        return self.unreadable

    @property
    def skipped(self) -> int:
        return self.unreadable


def _read_ttl(job_dir: Path) -> tuple[Optional[dict], Optional[bool], Optional[float], Optional[str]]:
    """Read the optional ttl.json: (raw, allow_stop, ttl_min, error). Absent file = no marker."""
    try:
        with open(job_dir / "ttl.json", "r", encoding="utf-8") as tf:
            raw = json.load(tf)
    except FileNotFoundError:
        return None, None, None, None
    except (OSError, ValueError) as exc:
        return None, False, None, f"ttl.json unreadable: {type(exc).__name__}"
    allow_stop, ttl_min, error = parse_ttl_marker(raw)
    return (raw if isinstance(raw, dict) else None), allow_stop, ttl_min, error


def read_session(job_dir: Union[str, Path]) -> Optional[SessionRecord]:
    """Read one job dir into a record, or None when state.json is missing/corrupt."""
    job_dir = Path(job_dir)
    try:
        with open(job_dir / "state.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    # Only a real updatedAt counts: createdAt is hours older for a session that
    # just finished, so falling back to it would stop that session at once.
    updated_at = parse_iso_dt(data.get("updatedAt"))

    raw_name = data.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""

    output = data.get("output")
    has_result = bool(isinstance(output, dict) and output.get("result") is not None)

    ttl, allow_stop, ttl_min, ttl_error = _read_ttl(job_dir)

    return SessionRecord(
        short_id=str(data.get("daemonShort") or job_dir.name),
        name=name,
        state=str(data.get("state") or ""),
        tempo=str(data.get("tempo") or ""),
        updated_at=updated_at,
        has_result=has_result,
        job_dir=job_dir,
        ttl=ttl,
        allow_stop=allow_stop,
        ttl_min=ttl_min,
        ttl_error=ttl_error,
    )


def scan(jobs_dir: Union[str, Path] = "~/.claude/jobs") -> ScanResult:
    """Read all jobs/*/state.json under jobs_dir, tolerating corrupt/partial files."""
    base = Path(jobs_dir).expanduser()
    records: list[SessionRecord] = []
    unreadable = 0

    if not base.is_dir():
        return ScanResult(records=[], unreadable=0)

    for state_file in sorted(base.glob("*/state.json")):
        record = read_session(state_file.parent)
        if record is None:
            unreadable += 1
        else:
            records.append(record)

    return ScanResult(records=records, unreadable=unreadable)


scan_jobs = scan
