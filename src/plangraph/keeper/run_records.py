"""Read persisted run records for snapshots without runtime imports or repairs."""
from __future__ import annotations

import json
from pathlib import Path


def read_run_records(state: Path):
    """Missing directories are unknown; invalid records retain unknown evidence."""
    directory = state / "runs"
    if not directory.is_dir():
        return None
    runs = []
    for path in sorted(directory.glob("run-*/record.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not isinstance(record.get("run_id"), str):
                raise ValueError("invalid run record")
        except (OSError, ValueError, UnicodeError):
            record = {"run_id": path.parent.name}
        runs.append(record)
    return runs
