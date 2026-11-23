"""Read persisted run records for snapshots without runtime imports or repairs."""
from __future__ import annotations

import json
from pathlib import Path


def read_run_records(state: Path):
    """Missing directories are unknown; invalid records retain unknown evidence."""
    return RunRecordCache().read(state)


def _stamp(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


class RunRecordCache:
    """Reuse the run listing and parse each record only when its file changes."""

    def __init__(self, *, fields=None):
        self.fields = fields
        self.directory = self.stamp = None
        self.paths = ()
        self.records = {}

    def read(self, state: Path):
        directory = state / "runs"
        stamp = _stamp(directory)
        if stamp is None or not directory.is_dir():
            self.directory, self.stamp, self.paths, self.records = directory, None, (), {}
            return None
        if directory != self.directory or stamp != self.stamp:
            self.directory, self.stamp = directory, stamp
            self.paths = tuple(path / "record.json" for path in sorted(directory.glob("run-*"))
                               if path.is_dir())
        current = {}
        for path in self.paths:
            version = _stamp(path)
            old = self.records.get(path)
            current[path] = old if old is not None and old[0] == version else (version, self._read(path))
        self.records = current
        return [record for _, record in current.values()]

    def _read(self, path):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not isinstance(record.get("run_id"), str):
                raise ValueError("invalid run record")
        except (OSError, ValueError, UnicodeError):
            record = {"run_id": path.parent.name}
        return record if self.fields is None else {key: record[key] for key in self.fields if key in record}
