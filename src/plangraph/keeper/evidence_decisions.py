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

import hashlib
import re
from pathlib import Path

__all__ = ["DecisionsAdapter", "DECISION_LINE_RE", "snapshot_texts"]

DECISION_LINE_RE = re.compile(r"^\| D-([0-9]+) \|")
_ASCII_DIGITS_RE = re.compile(r"[0-9]+\Z")
_FENCE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
# Match any keyword boundary: a word of letters/hyphens followed by : and then
# whitespace or end of cell.  Does NOT match project:ID (colon directly
# followed by non-space).
_KEYWORD_RE = re.compile(r"\b([A-Za-z-]+):(?=\s|$)")
# Waives:/Closes: keywords themselves match even without trailing space (old
# behaviour), but capture stops at the next _KEYWORD_RE boundary.
# FU-X7-LOWS: not after a letter, digit, ``-`` or ``_`` (``X-Waives:``,
# ``Pre-closes:`` are other words, not the keyword).
_WAIVES_RE = re.compile(r"(?i)(?<![\w-])waives\s*:")
_CLOSES_RE = re.compile(r"(?i)(?<![\w-])closes\s*:")
_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")
_WS_RE = re.compile(r"\s+")
# A whole ``project:ID`` key (the same shape tools/apply_batch.py accepts).
_KEY_TOKEN_RE = re.compile(r"[^:@/\s,]+:[^:@/\s,]+\Z")
_CLOSES_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}\Z")


def _parse_closes_token(token: str) -> tuple[str, str | None] | None:
    """Parse a Closes token into (key, sha|None). Returns None if invalid."""
    if "@" in token:
        parts = token.split("@")
        if len(parts) != 2:
            return None
        key, sha = parts
        if _KEY_TOKEN_RE.fullmatch(key) and _CLOSES_SHA_RE.fullmatch(sha):
            return key, sha
        return None
    if _KEY_TOKEN_RE.fullmatch(token):
        return token, None
    return None


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


def _text_from_rows(rows: dict[str, list[str]], decision_id) -> str | None:
    """``text()`` semantics on a given row map: the first non-empty DECISION cell."""
    canonical = DecisionsAdapter._canonical(decision_id)
    if canonical is None:
        return None
    hits = rows.get(canonical)
    if not hits:
        return None
    for row in hits:
        collapsed = _WS_RE.sub(" ", _decision_cell(row)).strip()
        if collapsed:
            return collapsed
    return ""


class DecisionsAdapter:
    """Read-only view of one decisions document, injected via ``ctx.decisions``."""

    def __init__(self, path: str | Path | None) -> None:
        self._path = Path(path) if path else None
        self._cache_stamp = None
        self._cache_rows: dict[str, list[str]] = {}

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

    def _stamp(self):
        """Cheap content digest of the decisions file, or ``None`` (never cached).

        Keys the cache - not ``(mtime_ns, size)``, which repeats across same-size
        edits on a coarse-mtime filesystem - and is far cheaper than parsing."""
        if self._path is None:
            return None
        try:
            data = self._path.read_bytes()
        except OSError:
            return None
        return hashlib.blake2b(data, digest_size=8).digest()

    def _cached_rows(self) -> dict[str, list[str]]:
        """``_rows()`` cached on a cheap content digest - SNAPSHOT READ PATH ONLY.

        ``store_read._decision_text`` (via ``snapshot_texts``) is the sole
        caller: the write-path enforcement methods (``has``, ``text``,
        ``waives``, ``closes``) must never read through this - a missed
        same-size edit would mis-authorise closes and waivers, so every one of
        them re-reads the file on every call.
        """
        stamp = self._stamp()
        if stamp is not None and stamp == self._cache_stamp:
            return self._cache_rows
        rows = self._rows()
        self._cache_stamp, self._cache_rows = stamp, rows
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

    def text(self, decision_id: str) -> str | None:
        """The DECISION cell of *decision_id*'s row, whitespace-collapsed."""
        return _text_from_rows(self._rows(), decision_id)

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

    def closes_with_commits(self, decision_id: str) -> dict[str, str | None]:
        """Map of key -> sha|None for valid ``Closes:`` tokens in *decision_id*'s rows."""
        canonical = self._canonical(decision_id)
        if canonical is None:
            return {}
        result: dict[str, str | None] = {}
        invalid: set[str] = set()
        for row in self._rows().get(canonical, ()):
            cell = _decision_cell(row)
            if not cell:
                continue
            keyword_positions = [(m.start(), m.group()) for m in _KEYWORD_RE.finditer(cell)]
            for match in _CLOSES_RE.finditer(cell):
                start = match.end()
                end = len(cell)
                for kw_pos, kw_text in keyword_positions:
                    if kw_pos > match.start():
                        end = kw_pos
                        break
                token_text = cell[start:end].strip()
                for token in _TOKEN_SPLIT_RE.split(token_text):
                    if not token:
                        continue
                    parsed = _parse_closes_token(token)
                    if parsed is None:
                        continue
                    key, sha = parsed
                    if key in invalid:
                        continue
                    if key not in result:
                        result[key] = sha
                    else:
                        prev = result[key]
                        if prev is None:
                            result[key] = sha
                        elif sha is None:
                            pass
                        else:
                            p_l, s_l = prev.lower(), sha.lower()
                            if p_l.startswith(s_l):
                                pass
                            elif s_l.startswith(p_l):
                                result[key] = sha
                            else:
                                invalid.add(key)
                                del result[key]
        return result

    def closes(self, decision_id: str) -> set[str]:
        """The whole ``Closes: <project:ID>`` keys in the DECISION cell of *decision_id*'s rows.

        The empty set means the row names nothing; only when the cell has no
        ``Closes:`` keyword at all (``has_closes_keyword``) does the hashed batch
        file alone authorise a decision close (X7b).
        """
        return set(self.closes_with_commits(decision_id).keys())

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


def snapshot_texts(adapter, ids):
    """``{id: cell text}`` for the ids the snapshot ``decision_text`` cites.

    The snapshot read path only (``store_read._decision_text``): a real
    :class:`DecisionsAdapter` parses the decisions file once per file version
    through ``_cached_rows`` (keyed on a cheap content digest) and answers every
    id from that one parse, while the write-path enforcement methods stay
    uncached; any other adapter (a test fake or subclass) keeps the per-id
    ``text()`` loop.  Same rules as ``text()`` plus the snapshot's 600-char cap.
    """
    if type(adapter) is DecisionsAdapter:   # exact type: a subclass overriding
        rows = adapter._cached_rows()       # text() must keep its own loop

        def lookup(did):
            return _text_from_rows(rows, did)
    else:
        lookup = getattr(adapter, "text", None)
        if not callable(lookup):
            return {}
    result: dict[str, str] = {}
    for did in ids:
        try:
            value = lookup(did)
        except Exception:  # noqa: BLE001 - one bad row skips
            continue
        if not isinstance(value, str) or not value:
            continue
        result[did] = value[:600]
    return result
