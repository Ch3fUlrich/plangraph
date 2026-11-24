"""Decision adapter: does ``| D-NNN |`` exist in the decisions authority?

Keeper contexts read the configured file's repository-relative path from a
freshly fetched origin/main, never the checkout. Direct parser users retain
the local-document view unless ``from_origin=True`` is explicitly requested.

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

Lists are read after an unquoted keyword, up to prose or the next ``Word:``
label. A middle dot can introduce another entry after an explanation.
"""

from __future__ import annotations

import contextlib
import threading
import os
import re
import subprocess
from pathlib import Path

from . import timing

__all__ = ["DecisionsAdapter", "DECISION_LINE_RE"]

DECISION_LINE_RE = re.compile(r"^\| D-([0-9]+) \|")
_ASCII_DIGITS_RE = re.compile(r"[0-9]+\Z")
_FENCE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
# Match any keyword boundary: a word of letters/hyphens followed by : and then
# whitespace or end of cell.  Does NOT match project:ID (colon directly
# followed by non-space).
_KEYWORD_RE = re.compile(r"(?<![^\s(])[A-Za-z-]+:(?=\s|$)")
# Waives:/Closes: keywords themselves match even without trailing space (old
# behaviour), but capture stops at the next _KEYWORD_RE boundary.
# FU-X7-LOWS: not after a letter, digit, ``-`` or ``_`` (``X-Waives:``,
# ``Pre-closes:`` are other words, not the keyword).
_WAIVES_RE = re.compile(r"(?i)(?<![\w-])waives\s*:")
_CLOSES_RE = re.compile(r"(?i)(?<![\w-])closes\s*:")
_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")
_LIST_ENTRY_RE = re.compile(r"\s+·\s+")
_TOKEN_PUNCTUATION = ".,;:)]'\"`"
_QUOTED_TEXT_RE = re.compile(r"""\x60[^\x60]*\x60|"[^"]*"|(?<!\w)'[^']*'""")
_WS_RE = re.compile(r"\s+")
# A whole ``project:ID`` key (the same shape tools/apply_batch.py accepts).
_KEY_TOKEN_RE = re.compile(r"[^:@/\s,]+:[^:@/\s,]+\Z")
_CLOSES_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}\Z")

# One part-table entry: "Batch 23, part head <sha>", "Batch 13, PR #1084, head <sha>", "Batch 26, heads <sha> and fix <sha>".
_HEX = r"`?[0-9a-f]{7,40}(?![\w])`?"
_PART_HEADS = re.compile(r"(?<![\w-])Batch\s+\d+\w*(?:\s*\([^)]*\))?,\s*(?:PR\s*#\d+,\s*)?(?:(?:part|lane)\s+)?heads?\s+"
                         rf"({_HEX}(?:(?:,\s*|\s+and\s+(?:fix\s+)?){_HEX})*)")

# Row 40 "supersedes / corrects / withdraws / replaces D-12" retires D-12.
_SUPERSEDES_RE = re.compile(
    r"(?i)\b(?:supersedes|corrects|withdraws|replaces)\s+D-0*(\d+)\b")
# Row 12 whose DECISION cell opens with "WITHDRAWN by D-40" / "superseded by
# D-40" retires itself; the same phrase later in a cell may be about anything.
_OWN_RETIRED_RE = re.compile(
    r"(?i)^\W{0,3}(?:superseded|withdrawn|replaced|corrected)\s+by\s+D-0*(\d+)\b")


def _did_num(did):
    try:
        return int(str(did).split("-", 1)[1])
    except (IndexError, ValueError):
        return -1


def superseded_map(rows):
    """``{"D-old": "D-new"}``: decisions a later row retires.

    Row ``D-new`` says ``supersedes|corrects|withdraws|replaces D-old``, or
    row ``D-old``'s DECISION cell opens with ``WITHDRAWN|superseded by D-new``.
    Only the DECISION cell (3rd column) is read.
    """
    out = {}
    for did, lines in rows.items():
        own = _did_num(did)
        for line in lines:
            cell = _decision_cell(line)
            match = _OWN_RETIRED_RE.match(cell)
            if match and int(match.group(1)) > own:
                out.setdefault(did, f"D-{int(match.group(1))}")
            for found in _SUPERSEDES_RE.finditer(cell):
                old = f"D-{int(found.group(1))}"
                newest = out.get(old)
                if _did_num(old) < own and (newest is None or _did_num(newest) < own):
                    out[old] = did
    return out


class DecisionsFetchFailed(OSError):
    """The decisions repository could not fetch origin/main."""


class DecisionsOriginUnreadable(OSError):
    """The configured repository or origin/main decisions blob is unreadable."""


def _global_credential_entries() -> list[tuple[str, str]]:
    """The caller's global credential helpers, read without following includes.

    Only these keys cross into the isolated fetch env; values are never logged.
    """
    try:
        proc = subprocess.Popen(
            ["git", "config", "--global", "--no-includes", "--null", "--get-regexp", r"^credential\..*helper$"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            out, _ = proc.communicate(timeout=timing.io_timeout(10))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    entries = []
    for record in out.decode("utf-8", "replace").split("\0"):
        key, sep, value = record.partition("\n")
        if sep and key.startswith('credential.') and key.endswith('.helper'):
            entries.append((key, value))
    return entries


def _origin_git_env() -> dict[str, str]:
    """Ignore caller repository/config overrides; retain transport safety guards.

    The caller's global credential helpers are re-injected via GIT_CONFIG_COUNT so a
    private https origin can still authenticate.
    """
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_") or name in {"GIT_ALLOW_PROTOCOL", "GIT_TERMINAL_PROMPT"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    entries = _global_credential_entries()
    if entries:
        env["GIT_CONFIG_COUNT"] = str(len(entries))
        for index, (key, value) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def read_decisions_origin(path: Path) -> str:
    """Discover the repo, fetch main and read its configured blob; never fall back."""
    env = _origin_git_env()
    try:
        path = path.resolve()
        root = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, env=env, timeout=timing.io_timeout(30)).stdout.decode("utf-8").strip()
        repo = Path(root).resolve()
        relative = path.relative_to(repo).as_posix()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise DecisionsOriginUnreadable from None
    try:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin",
             "+refs/heads/main:refs/remotes/origin/main"],
            check=True, capture_output=True, env=env, timeout=timing.io_timeout(60))
    except (OSError, subprocess.SubprocessError):
        raise DecisionsFetchFailed from None
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", f"origin/main:{relative}"],
            check=True, capture_output=True, env=env, timeout=timing.io_timeout(30)).stdout.decode("utf-8")
    except (OSError, ValueError, subprocess.SubprocessError):
        raise DecisionsOriginUnreadable from None


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


def _listed_tokens(cell: str, pattern: re.Pattern):
    """Yield normalized list tokens without consuming the surrounding prose."""
    quotes = [match.span() for match in _QUOTED_TEXT_RE.finditer(cell)]
    for match in pattern.finditer(cell):
        start = match.start()
        if (start and cell[start - 1] in "'\"`") or any(
                left <= start < right for left, right in quotes):
            continue
        boundary = _KEYWORD_RE.search(cell, match.end())
        end = boundary.start() if boundary else len(cell)
        for entry in _LIST_ENTRY_RE.split(cell[match.end():end]):
            found = False
            for raw in _TOKEN_SPLIT_RE.split(entry):
                token = raw.strip(_TOKEN_PUNCTUATION)
                if not token:
                    continue
                key = token.partition("@")[0]
                if key.startswith(("(", "[")) or not _KEY_TOKEN_RE.fullmatch(key):
                    # A middle dot can resume the list after a parenthetical
                    # explanation attached to an entry, or after a bare key.
                    if not (raw.startswith("(") and entry.rstrip().endswith(")")):
                        found = False
                    break
                found = True
                yield token
            if not found:
                break


def go_part_entries(statement):
    """Read part-table entries, keeping each match span for its key scope."""
    return _PART_HEADS.finditer(statement)


def go_part_tokens(entry):
    """Return the revision tokens named by one GO part-table entry."""
    return re.findall(r"[0-9a-f]{7,40}", entry[1])


def close_commit_scope(cell):
    """Return the key and revision bindings from one decision cell."""
    return _close_commit_tokens(_listed_tokens(cell, _CLOSES_RE))


def _close_commit_tokens(tokens):
    result: dict[str, str | None] = {}
    invalid: set[str] = set()
    for token in tokens:
        parsed = _parse_closes_token(token)
        if parsed is None:
            continue
        key, sha = parsed
        if key in invalid:
            continue
        prev = result.get(key)
        if prev is None:
            result[key] = sha
        elif sha is not None:
            p_l, s_l = prev.lower(), sha.lower()
            if s_l.startswith(p_l) and len(sha) > len(prev):
                result[key] = sha
            elif not p_l.startswith(s_l):
                invalid.add(key)
                del result[key]
    return result


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

    def __init__(self, path: str | Path | None, *, from_origin: bool = False) -> None:
        self._path = Path(path) if path else None
        self._from_origin = from_origin
        self._local = threading.local()

    @contextlib.contextmanager
    def snapshot(self):
        """Serve every read inside the block from one read and parse of the document.

        The snapshot belongs to the calling thread: a nested block shares the
        thread's outer snapshot, a block entered after the outer one closed
        reads the document again, and another thread's blocks never share it.
        """
        local = self._local
        if getattr(local, "depth", 0) == 0:
            local.snapshot = {}
        local.depth = getattr(local, "depth", 0) + 1
        try:
            yield self
        finally:
            local.depth -= 1
            if local.depth == 0:
                local.snapshot = None

    def _rows_strict(self) -> dict[str, list[str]]:
        return self._rows(_strict=True)

    def _document(self, *, _strict=False):
        snap = getattr(self._local, "snapshot", None)
        if snap is not None and "text" in snap:
            return snap["text"]
        if snap is None:
            return self._read_document(_strict=_strict)
        # Inside a snapshot every read is strict, so the cached text can serve a
        # strict caller; a best-effort caller falls back to the lenient read
        # without caching it.
        try:
            text = self._read_document(_strict=True)
        except Exception:  # noqa: BLE001
            if _strict:
                raise
            return self._read_document(_strict=False)
        snap["text"] = text
        return text

    def _read_document(self, *, _strict=False):
        if self._path is None:
            return ""
        if self._from_origin:
            text = read_decisions_origin(self._path)
        else:
            if not self._path.is_file():
                if _strict:
                    raise OSError("decisions path is not a regular file")
                return ""
            try:
                text = self._path.read_text(encoding="utf-8")
            except OSError:
                if _strict:
                    raise
                return ""
        return text

    def seat_waiver_cutoff(self):
        """Rule 3.37's sole ISO date; absent rule uses the historical default.

        Read the same authoritative document as decisions, including origin
        mode. A present ambiguous/malformed rule must never use the default.
        """
        rows = []
        for line in _prose_lines(self._document(_strict=True)):
            if re.match(r"^\s*\|\s*`?3\.37`?\s*\|", line):
                rows.append(line)
        if not rows:
            return "2026-11-13"
        dates = re.findall(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])", rows[0])
        if len(rows) != 1 or len(dates) != 1:
            raise ValueError("rule 3.37 cutoff unavailable or ambiguous")
        return dates[0]

    def fields(self, decision_id):
        """Unique row by named table columns; absent/ambiguous headers refuse."""
        canonical = self._canonical(decision_id)
        header, found = None, []
        for line in _prose_lines(self._document(_strict=True)):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            names = [c.lower() for c in cells]
            if len(names) >= 3 and names[2] == "decision":
                header = names if len(set(names)) == len(names) else None
            match = DECISION_LINE_RE.match(line)
            if match and f"D-{int(match[1])}" == canonical:
                found.append(dict(zip(header, cells)) if header and len(header) == len(cells) else {})
        return found[0] if len(found) == 1 else {}

    def _rows(self, *, _strict=False) -> dict[str, list[str]]:
        """Normalised prose rows; origin failures raise, local reads can be best effort."""
        text = self._document(_strict=_strict)
        snap = getattr(self._local, "snapshot", None)
        if snap is not None and "rows" in snap and snap["rows"][0] is text:
            return snap["rows"][1]
        rows: dict[str, list[str]] = {}
        for line in _prose_lines(text):
            match = DECISION_LINE_RE.match(line)
            if match:
                rows.setdefault(f"D-{int(match.group(1))}", []).append(line)
        if snap is not None:
            snap["rows"] = (text, rows)
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
        canonical = self._canonical(decision_id)
        if canonical is None:
            return None
        rows = self._rows().get(canonical)
        if not rows:
            return None
        for row in rows:
            collapsed = _WS_RE.sub(" ", _decision_cell(row)).strip()
            if collapsed:
                return collapsed
        return ""

    def _scoped_tokens(self, decision_id: str, pattern: re.Pattern):
        """The whole tokens in the DECISION cell of *decision_id*'s rows."""
        canonical = self._canonical(decision_id)
        if canonical is not None:
            for row in self._rows().get(canonical, ()):
                yield from _listed_tokens(_decision_cell(row), pattern)

    def waives(self, decision_id: str, key: str) -> bool:
        """Does the DECISION cell of *decision_id*'s row name *key* as a whole ``Waives:`` token?"""
        if not isinstance(key, str) or not key.strip():
            return False
        return key.strip() in self._scoped_tokens(decision_id, _WAIVES_RE)

    def closes_with_commits(self, decision_id: str) -> dict[str, str | None]:
        """Map of key -> sha|None for valid ``Closes:`` tokens in *decision_id*'s rows."""
        return _close_commit_tokens(self._scoped_tokens(decision_id, _CLOSES_RE))

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


# ------------------------------------------------------------ decisions
_BOOK_LOCK = threading.Lock()
_BOOKS = {}
_BOOK_SLOTS = 3
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]+")


class DecisionsUnreadable(Exception):
    """The decisions authority could not be read for enforcement."""


def decision_book(decisions, *, fresh=False):
    """``{"rows", "superseded", "index"}`` for a :class:`DecisionsAdapter`.

    ``rows`` = ``{"D-n": [row lines]}``, ``superseded`` = :func:`superseded_map`
    and ``index`` = ``{token: {D-ids}}`` over every key-like token, so naming
    lookups cost one dict probe.  Built once per file version (path, inode,
    mtime, size; up to three files kept); an unreadable source is an empty book.
    ``fresh`` bypasses the cache for enforcement without replacing read books.
    """
    if decisions is None:
        return _build_book({})
    if fresh:
        try:
            rows = getattr(decisions, "_rows_strict", getattr(decisions, "_rows", dict))()
        except (OSError, UnicodeDecodeError):
            raise DecisionsUnreadable from None
        return _build_book({did: tuple(lines) for did, lines in rows.items()})
    # Origin-backed rows change independently of the working-tree file's stat.
    path = None if getattr(decisions, "_from_origin", False) else getattr(decisions, "_path", None)
    try:
        stat = os.stat(path) if path is not None else None
    except OSError:
        stat = None
    if stat is None:             # no file identity: parse, but do not cache
        return _build_book(_read_rows(decisions))
    stamp = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    with _BOOK_LOCK:
        book = _BOOKS.get(stamp)
    if book is not None:
        return book
    book = _build_book(_read_rows(decisions))
    with _BOOK_LOCK:
        for old in [k for k in _BOOKS if k[0] == stamp[0]]:
            del _BOOKS[old]
        while len(_BOOKS) >= _BOOK_SLOTS:
            del _BOOKS[next(iter(_BOOKS))]
        _BOOKS[stamp] = book
    return book


def _read_rows(decisions):
    try:
        return {did: tuple(lines) for did, lines in decisions._rows().items()}
    except Exception:
        return {}


def _build_book(rows):
    index = {}
    for did, lines in rows.items():
        for line in lines:
            for token in _TOKEN_RE.findall(line):
                token = token.strip(".:")
                if "-" in token or ":" in token:     # key-like only
                    index.setdefault(token, set()).add(did)
    return {"rows": rows, "superseded": superseded_map(rows), "index": index}


def decisions_naming(book, key):
    """Decision ids whose row names *key*, sorted by number.

    The full ``project:ID`` key always counts; the bare id counts when it is
    distinctive (contains ``-`` and is at least 6 chars), since rows often
    drop the project prefix.  A longer id (``X-FU``) is a different token.
    """
    bare = key.split(":", 1)[-1]
    hits = set(book["index"].get(key, ()))
    if "-" in bare and len(bare) >= 6:
        hits |= book["index"].get(bare, set())
    return sorted(hits, key=_did_num)


# ------------------------------------------------- premise precision
# KEEPER-STALE-PREMISE-PRECISION.  The log row has no class column, no date and
# no session, so the classes are read from the row text.  A row that cannot be
# classified stays subject to the gate; ``reconfirm`` is the way to clear it.
_ORDERS_ITEM = re.compile(r"(?i:\border\w*|\bdefin\w+|\bqueue\w*|\bcreat\w+|\bprioriti\w+|\bsequenc\w+)|\bGO\b")
_CHANGES_SCOPE = re.compile(
    r"(?i)\b(?:supersed\w*|replac\w*|withdr\w*|correct\w*|cancel\w*|drop\w*|narrow\w*|widen\w*"
    r"|rescop\w*|obsolete\w*|revert\w*|no longer|instead of|now also|now covers|scope|premise"
    # a GO or order that adds a condition changes what the item must do first
    r"|requir\w*|approv\w*|need\w*|must|before|unless|only|except|condition\w*|block\w*|hold|wait\w*"
    r"|not|but|also|second|additional)\b")


#: Words an ordering row may carry besides item keys, a stamp and at most ``_TERSE_LEFTOVER`` others.
_ORDER_WORDS = frozenset((
    "go order ordered orders ordering operator router define defined defines queue queued queues "
    "create created priority prioritize prioritise prioritized prioritised sequence sequenced first "
    "next now the a an is are was be and or in of to for on at it its this that as by with plan plans "
    "item items lesson lessons").split())
_TERSE_LEFTOVER = 2


def _terse_order(lines):
    """Is the row only an order of the item: after the stamp, keys and ordering words at most two
    other words remain?  A row that says more (a backend change, a new rule) is read by a human."""
    cells = " ".join(lines).split("|")
    text = re.sub(r"\(\d\d:\d[\dx]Z[^)]*\)", " ", cells[3] if len(cells) > 3 else " ".join(cells))
    text = re.sub(r"[\w.-]+:[\w.-]+|D-\d+", " ", text)
    left = [w for w in re.findall(r"[A-Za-z]+", text) if w.lower() not in _ORDER_WORDS]
    return len(left) <= _TERSE_LEFTOVER


def premise_neutral(lines, did, source):
    """May the decision *did* (its row *lines*) be ignored by the premise gate?

    (a) it is the item's own ``source`` decision, or (b) its row orders, GOes or
    defines the item and says nothing that changes scope.  The operator's third
    case, "logged within two hours of the define by the same session", needs the
    session and the full date, which the log does not carry: a time of day and an
    id gap would let a row of another day or session mask a later scope change,
    so that case is not guessed.  The ordering row must also be terse (a keyword
    list cannot tell an order from an order that changes a backend).
    """
    if did in re.findall(r"D-\d+", source or ""):
        return True
    text = " ".join(lines)
    return (not _CHANGES_SCOPE.search(text) and bool(_ORDERS_ITEM.search(text))
            and _terse_order(lines))
