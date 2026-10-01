"""Decision adapter: does ``| D-NNN |`` exist in the configured DECISIONS.md?

The contract's marker is the literal line start ``^\\| D-(\\d+) \\|`` — a table
row with spaces around the id. The comparison normalises ``D-0209`` / ``d-209``
to ``D-209`` so a padded or lower-case reference still matches.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["DecisionsAdapter", "DECISION_LINE_RE"]

DECISION_LINE_RE = re.compile(r"^\| D-(\d+) \|")


class DecisionsAdapter:
    """Read-only view of one decisions document, injected via ``ctx.decisions``."""

    def __init__(self, path: str | Path | None) -> None:
        self._path = Path(path) if path else None

    def decisions(self) -> set[str]:
        """Ids present, normalised to ``D-<int>`` (empty when unconfigured/missing)."""
        if self._path is None or not self._path.is_file():
            return set()
        found: set[str] = set()
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return set()
        for line in text.splitlines():
            match = DECISION_LINE_RE.match(line)
            if match:
                found.add(f"D-{int(match.group(1))}")
        return found

    def has(self, decision_id: str) -> bool:
        if not isinstance(decision_id, str):
            return False
        raw = decision_id.strip().upper()
        if raw.startswith("D-"):
            raw = raw[2:]
        if not raw.isdigit():
            return False
        return f"D-{int(raw)}" in self.decisions()

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return "DecisionsAdapter()"
