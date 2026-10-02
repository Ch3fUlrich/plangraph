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
satisfies ``demo:AB``; one row may carry several ``Waives:`` tokens.  Per router
decision D-276, ``Waives:`` and ``Closes:`` tokens count only inside the
DECISION cell (3rd column) of their D-row — a token in any other cell is
ignored.  ``closes(decision_id)`` follows the same whole-token shape.

Keyword capture stops at the next ``Word:`` (letters/hyphens followed by ``:`` and
then whitespace or end of cell).  ``project:ID`` tokens (colon directly followed
by a non-space) do not act as boundaries.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["DecisionsAdapter", "DECISION_LINE_RE"]

DECISION_LINE_RE = re.compile(r"^\| D-([0-9]+) \|")
_ASCII_DIGITS_RE = re.compile(r"[0-9]+\Z")
_FENCE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
# Match any keyword boundary: a word of letters/hyphens followed by : and then
# whitespace or end of cell.  Does NOT match project:ID (colon directly
# followed by non-space).
_KEYWORD_RE = re.compile(r"\b([A-Za-z-]+):(?=\s|$)")
# Waives:/Closes: keywords themselves match even without trailing space (old
# behaviour), but capture stops at the next _KEYWORD_RE boundary.
_WAIVES_RE = re.compile(r"\bWaives:")
_CLOSES_RE = re.compile(r"\bCloses:")
_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")
# A whole ``project:ID`` key (the same shape tools/apply_batch.py accepts).
_KEY_TOKEN_RE = re.compile(r"[^:@/\s,]+:[^:@/\s,]+\Z")


def _decision_cell(row: str) -> str:
    """The DECISION cell (3rd column) of a ``|`` table row (``""`` when short).

    Router decision D-276: ``Waives:``/``Closes:`` tokens bind only here, so a
    token quoted in the project/by/why cells carries no authority.
    """
    cells = row.split("|")[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    if len(cells) < 3:
        return ""
    return cells[2]


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

    def _scoped_tokens(self, decision_id: str, pattern: re.Pattern) -> set[str]:
        """The whole tokens in the DECISION cell of *decision_id*'s rows.

        Tokens are captured from each Waives:/Closes: keyword until the next
        keyword (any Word: followed by whitespace or end of cell) or end of cell.
        """
        canonical = self._canonical(decision_id)
        if canonical is None:
            return set()
        tokens: set[str] = set()
        for row in self._rows().get(canonical, ()):
            cell = _decision_cell(row)
            if not cell:
                continue
            # Find all keyword positions in the cell
            keyword_positions = [(m.start(), m.group()) for m in _KEYWORD_RE.finditer(cell)]
            # For each match of our target pattern (Waives: or Closes:)
            for match in pattern.finditer(cell):
                start = match.end()  # Position after "Waives:" or "Closes:"
                # Find the next keyword position after this match
                end = len(cell)
                for kw_pos, kw_text in keyword_positions:
                    if kw_pos > match.start():
                        end = kw_pos
                        break
                # Extract the text between the keyword and the next keyword
                token_text = cell[start:end].strip()
                # Split into individual tokens
                for token in _TOKEN_SPLIT_RE.split(token_text):
                    if token:
                        tokens.add(token)
        return tokens

    def waives(self, decision_id: str, key: str) -> bool:
        """Does the DECISION cell of *decision_id*'s row name *key* as a whole ``Waives:`` token?"""
        if not isinstance(key, str) or not key.strip():
            return False
        return key.strip() in self._scoped_tokens(decision_id, _WAIVES_RE)

    def closes(self, decision_id: str) -> set[str]:
        """The whole ``Closes: <project:ID>`` tokens in the DECISION cell of *decision_id*'s rows.

        The empty set means the row names nothing; only when the cell has no
        ``Closes:`` keyword at all (``has_closes_keyword``) does the hashed batch
        file alone authorise a decision close (X7b).
        """
        return {token for token in self._scoped_tokens(decision_id, _CLOSES_RE)
                if _KEY_TOKEN_RE.match(token)}

    def has_closes_keyword(self, decision_id: str) -> bool:
        """Does the DECISION cell carry a ``Closes:`` keyword at all (L5)?

        A keyword with no valid ``project:ID`` after it (``Closes: demo: A``,
        a bare ``Closes:``) is a malformed close, which ``close_on_decision``
        must refuse rather than treat as "names nothing".
        """
        canonical = self._canonical(decision_id)
        return canonical is not None and any(
            _CLOSES_RE.search(_decision_cell(row) or "")
            for row in self._rows().get(canonical, ()))

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return "DecisionsAdapter()"
