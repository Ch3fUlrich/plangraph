"""Decision adapter: does ``| D-NNN |`` exist in the configured DECISIONS.md?

The contract's marker is the literal line start ``^\\| D-([0-9]+) \\|`` — a table
row with spaces around the id.  ``[0-9]`` rather than ``\\d`` because the latter
also accepts non-ASCII digits, which are not valid decision ids.  The comparison
normalises ``D-0209`` / ``d-209`` to ``D-209`` so a padded or lower-case
reference still matches.

Fenced code blocks (``` or ~~~) and indented code blocks (four spaces or a tab)
are stripped before matching, so a decision id that only appears in an example
snippet does not count as recorded.

``waives(decision_id, key)`` is the waiver-binding rule (D-217 / X6c3): a D-row
must name the *scope* it waives as a whole ``Waives: <project:ID>`` token, so a
recorded decision cannot be reused to waive an unrelated item.  ``demo:A`` never
satisfies ``demo:AB``; one row may carry several ``Waives:`` tokens.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["DecisionsAdapter", "DECISION_LINE_RE"]

DECISION_LINE_RE = re.compile(r"^\| D-([0-9]+) \|")
_ASCII_DIGITS_RE = re.compile(r"[0-9]+\Z")
_FENCE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
_WAIVES_RE = re.compile(r"\bWaives:[ \t]*([^|\r\n]*)")
_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")


def _prose_lines(text: str):
    """Yield lines outside fenced and indented code blocks."""
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("    ") or line.startswith("\t"):
            continue
        yield line


class DecisionsAdapter:
    """Read-only view of one decisions document, injected via ``ctx.decisions``."""

    def __init__(self, path: str | Path | None) -> None:
        self._path = Path(path) if path else None

    def _rows(self) -> dict[str, list[str]]:
        """Prose table rows keyed by normalised id (empty when unreadable)."""
        if self._path is None or not self._path.is_file():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        rows: dict[str, list[str]] = {}
        for line in _prose_lines(text):
            match = DECISION_LINE_RE.match(line)
            if match:
                rows.setdefault(f"D-{int(match.group(1))}", []).append(line)
        return rows

    def decisions(self) -> set[str]:
        """Ids present, normalised to ``D-<int>`` (empty when unconfigured/missing)."""
        return set(self._rows())

    @staticmethod
    def _canonical(decision_id) -> str | None:
        """``D-0209``/``d-209`` -> ``D-209``; ``None`` when not a decision id."""
        if not isinstance(decision_id, str):
            return None
        raw = decision_id.strip().upper()
        if raw.startswith("D-"):
            raw = raw[2:]
        if not _ASCII_DIGITS_RE.fullmatch(raw):
            return None
        return f"D-{int(raw)}"

    def has(self, decision_id: str) -> bool:
        canonical = self._canonical(decision_id)
        return canonical is not None and canonical in self._rows()

    def waives(self, decision_id: str, key: str) -> bool:
        """Does the row for *decision_id* name *key* as a whole ``Waives:`` token?"""
        canonical = self._canonical(decision_id)
        if canonical is None or not isinstance(key, str) or not key.strip():
            return False
        wanted = key.strip()
        for row in self._rows().get(canonical, ()):
            for clause in _WAIVES_RE.finditer(row):
                for token in _TOKEN_SPLIT_RE.split(clause.group(1).strip()):
                    if token == wanted:
                        return True
        return False

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return "DecisionsAdapter()"
