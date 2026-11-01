"""log.py - append-only JSONL logging for session reap stops (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import socket
from typing import Any, Optional, Union

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
) -> dict[str, Any]:
    """Append one JSON line per stopped session (or error) to log_path.

    Only the specified fields are recorded:
    ts, host, short_id, name, age_min, state, reason, action, rc.
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
    }

    line = json.dumps(entry, sort_keys=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

    return entry
