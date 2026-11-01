"""scan.py - scan ~/.claude/jobs/*/state.json (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union


def parse_iso_dt(val: Any) -> Optional[datetime]:
    """Parse ISO UTC timestamp into a timezone-aware UTC datetime."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    if not isinstance(val, str) or not val.strip():
        return None
    s = val.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


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
    ttl_min: Optional[int] = None

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __iter__(self):
        return iter((
            self.short_id,
            self.name,
            self.state,
            self.tempo,
            self.updated_at,
            self.has_result,
        ))

    def __len__(self) -> int:
        return 6


class ScanResult(NamedTuple):
    records: list[SessionRecord]
    unreadable: int

    @property
    def unreadable_count(self) -> int:
        return self.unreadable

    @property
    def skipped(self) -> int:
        return self.unreadable


def scan(jobs_dir: Union[str, Path] = "~/.claude/jobs") -> ScanResult:
    """Read all jobs/*/state.json under jobs_dir, tolerating corrupt/partial files."""
    base = Path(jobs_dir).expanduser()
    records: list[SessionRecord] = []
    unreadable = 0

    if not base.is_dir():
        return ScanResult(records=[], unreadable=0)

    for state_file in sorted(base.glob("*/state.json")):
        job_dir = state_file.parent
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            unreadable += 1
            continue

        if not isinstance(data, dict):
            unreadable += 1
            continue

        raw_updated = data.get("updatedAt") or data.get("createdAt")
        updated_at = parse_iso_dt(raw_updated)
        if updated_at is None and raw_updated is not None:
            # Corrupted date string
            unreadable += 1
            continue

        state = str(data.get("state") or "")
        tempo = str(data.get("tempo") or "")
        name = str(data.get("name") or job_dir.name)
        short_id = str(data.get("daemonShort") or job_dir.name)

        output = data.get("output")
        has_result = bool(isinstance(output, dict) and output.get("result") is not None)

        # Check for optional ttl.json next to state.json
        ttl_data = None
        allow_stop = None
        ttl_min = None
        ttl_file = job_dir / "ttl.json"
        if ttl_file.is_file():
            try:
                with open(ttl_file, "r", encoding="utf-8") as tf:
                    raw_ttl = json.load(tf)
                if isinstance(raw_ttl, dict):
                    ttl_data = raw_ttl
                    if "allow_stop" in raw_ttl:
                        allow_stop = bool(raw_ttl["allow_stop"])
                    if "ttl_min" in raw_ttl:
                        try:
                            ttl_min = int(raw_ttl["ttl_min"])
                        except (ValueError, TypeError):
                            ttl_min = None
            except Exception:
                ttl_data = None

        record = SessionRecord(
            short_id=short_id,
            name=name,
            state=state,
            tempo=tempo,
            updated_at=updated_at,
            has_result=has_result,
            job_dir=job_dir,
            ttl=ttl_data,
            allow_stop=allow_stop,
            ttl_min=ttl_min,
        )
        records.append(record)

    return ScanResult(records=records, unreadable=unreadable)


scan_jobs = scan
