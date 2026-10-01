"""Run-record adapter: reads ``<runs_dir>/<run_id>/{job,exit}.json``.

The run id is validated against ``^[A-Za-z0-9._-]+$`` *before* it is joined
onto the runs directory, so ``..`` and any slash can never traverse out of it.
Resolution is checked again anyway: the run directory must stay inside a
resolved ``runs_dir`` and may not itself be a symlink, and neither ``job.json``
nor ``exit.json`` may be a symlink — a symlink would otherwise let a run record
outside ``runs_dir`` verify.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["RunRecordAdapter", "RUN_ID_RE"]

RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RunRecordAdapter:
    """Filesystem adapter injected through ``ctx.runs``."""

    def __init__(self, runs_dir: str | Path | None) -> None:
        self._runs_dir = Path(runs_dir) if runs_dir else None

    def valid_run_id(self, run_id: object) -> bool:
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            return False
        return run_id not in (".", "..")

    def run_dir(self, run_id: str) -> Path:
        if not self.valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        if self._runs_dir is None:
            raise FileNotFoundError("runs_dir is not configured")
        candidate = self._runs_dir / run_id
        if candidate.is_symlink():
            raise ValueError(f"refusing symlinked run dir: {run_id!r}")
        if not candidate.resolve().is_relative_to(self._runs_dir.resolve()):
            raise ValueError(f"run dir escapes runs_dir: {run_id!r}")
        return candidate

    def job(self, run_id: str) -> dict:
        return self._load(self.run_dir(run_id) / "job.json")

    def exit(self, run_id: str) -> dict:
        return self._load(self.run_dir(run_id) / "exit.json")

    @staticmethod
    def _load(path: Path) -> dict:
        if path.is_symlink():
            raise ValueError(f"refusing symlinked run file: {path.name}")
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"{path.name} is not a JSON object")
        return data

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return "RunRecordAdapter()"
