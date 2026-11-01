"""log.py - append-only JSONL logging for session reap stops (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import socket
from typing import Any, Optional, Union

from .scan import parse_iso_dt

DEFAULT_LOG_PATH = Path("~/.local/state/plangraph/session-reap.jsonl").expanduser()


def log_stop(
    short_id: str,
    name: str,
    age_min: float,
    state: str,
    reason: str,
    action: str = "stop",
    rc: int = 0,
    log_path: Union[str, Path] = DEFAULT_LOG_PATH,
    host: Optional[str] = None,
    ts: Optional[str] = None,
    updated_at: Union[datetime, str, None] = None,
) -> dict[str, Any]:
    """Append one JSON line per stopped session (or error) to log_path.

    Only the specified fields are recorded:
    ts, host, short_id, name, age_min, state, reason, action, rc, updated_at.
    `updated_at` is the session record's updatedAt (ISO 8601 with offset): `claude stop` leaves
    state.json unchanged, so this is how a later run recognises a session it already stopped.
    No env values, no conversation text.
    """
    path = Path(log_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = ts or datetime.now(timezone.utc).isoformat()
    hostname = host or socket.gethostname()

    # Normalize age_min
    if isinstance(age_min, float):
        normalized_age = round(age_min, 1)
    else:
        try:
            normalized_age = round(float(age_min), 1)
        except (ValueError, TypeError):
            normalized_age = 0.0

    entry: dict[str, Any] = {
        "ts": timestamp,
        "host": hostname,
        "short_id": str(short_id),
        "name": str(name),
        "age_min": normalized_age,
        "state": str(state),
        "reason": str(reason),
        "action": str(action),
        "rc": int(rc),
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }

    line = json.dumps(entry, sort_keys=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

    return entry


def stopped_updated_at(log_path: Union[str, Path]) -> dict[str, datetime]:
    """short_id -> updated_at of that id's last successful (rc 0) stop line in the log.

    A missing or unreadable file is empty; a malformed line, one without a parseable
    offset-bearing `updated_at` (lines written before the field existed) or without an
    integer rc is skipped. A stop that failed (rc != 0) never counts, so it is retried.
    """
    try:
        text = Path(log_path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    stopped: dict[str, datetime] = {}
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        short_id, rc = entry.get("short_id"), entry.get("rc")
        if not isinstance(short_id, str) or isinstance(rc, bool) or rc != 0:
            continue
        updated = parse_iso_dt(entry.get("updated_at"))
        if updated is not None:
            stopped[short_id] = updated
    return stopped
