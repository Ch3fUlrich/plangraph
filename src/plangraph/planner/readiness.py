"""Readiness gate for planner cards (design 4.2): pure code, no model, $0.

A card is a plain dict (the shape ``Store.put_card`` stores).  ``check_card`` collects every
reason the card is not ready to run into a ``Verdict``; nothing here touches the store, the
network or the clock.  The check command is never executed: ``default_dry_run`` only looks
at the command line and the files it names.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from plangraph.keeper.schema import WINDOW_CLASSES

__all__ = ["WINDOW_TOKENS", "USABLE_FRACTION", "CODES", "Finding", "Verdict",
           "paths_overlap", "write_sets_disjoint", "default_dry_run", "check_card",
           "check_cards"]

#: Context window per class, in tokens.
WINDOW_TOKENS = {"128k": 131072, "256k": 262144, "1m": 1048576}
#: Share of the window a single leg may use.
USABLE_FRACTION = 0.55
#: Every finding code ``check_card`` can emit.
CODES = ("no_check", "check_not_runnable", "read_missing", "write_overlap",
         "estimate_missing", "estimate_too_big", "leg_request_too_big", "no_risk_tags",
         "no_window_class", "bad_path", "no_heldout")

_GLOB_CHARS = "*?["
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SEP_RE = re.compile(r"[;|&()]+\Z")
_SEGMENT_CAP = 5
_PYTHONS = re.compile(r"python(3(\.\d+)?)?\Z")
_PYTEST_VALUE_OPTS = frozenset(("-k", "-m", "-p", "-c", "-o"))
#: Shell builtins that ``shutil.which`` does not find.
_BUILTINS = frozenset((
    "cd", "export", "set", "unset", "source", ".", ":", "exit", "true", "false", "test",
    "[", "echo", "type", "umask", "wait", "eval", "read", "ulimit"))
_EMPTY: Mapping[str, Sequence[str]] = MappingProxyType({})


@dataclass(frozen=True)
class Finding:
    """One reason a card is not ready."""

    code: str
    message: str


@dataclass(frozen=True)
class Verdict:
    """Outcome of gating one card: ready when there are no findings."""

    key: str | None
    findings: tuple[Finding, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.findings

    def codes(self) -> list[str]:
        """Distinct finding codes in first-seen order."""
        out: list[str] = []
        for f in self.findings:
            if f.code not in out:
                out.append(f.code)
        return out

    def summary(self) -> str:
        if self.ready:
            return f"{self.key}: ready"
        return f"{self.key}: not ready - {','.join(self.codes())}"


# ---------------------------------------------------------------- path overlap

def _norm(path: str) -> str:
    p = str(path).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/{2,}", "/", p)
    p = p.rstrip("/")
    return "" if p == "." else p


def _is_glob(path: str) -> bool:
    return any(c in path for c in _GLOB_CHARS)


def _parts(path: str) -> list[str]:
    return [c for c in path.split("/") if c and c != "."]


def _literal_prefix(path: str) -> list[str]:
    out: list[str] = []
    for comp in _parts(path):
        if _is_glob(comp):
            break
        out.append(comp)
    return out


def _prefix_related(a: list[str], b: list[str]) -> bool:
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _glob_re(pattern: str) -> re.Pattern[str]:
    """Conservative glob to regex: ``*`` crosses ``/``, ``**/`` may match zero dirs."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            while i < n and pattern[i] == "*":
                i += 1
            out.append(".*")
            continue
        if c == "?":
            out.append(".")
        elif c == "[":
            j = pattern.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def paths_overlap(a: str, b: str) -> bool:
    """Conservative, symmetric check whether two paths/globs may name a common file."""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    ga, gb = _is_glob(a), _is_glob(b)
    if not ga and not gb:
        return _prefix_related(_parts(a), _parts(b))
    if ga != gb:
        glob, lit = (a, b) if ga else (b, a)
        if _glob_re(glob).match(lit):
            return True
        lp, gp = _parts(lit), _literal_prefix(glob)
        return len(lp) <= len(gp) and gp[:len(lp)] == lp
    if _glob_re(a).match(b) or _glob_re(b).match(a):
        return True
    return _prefix_related(_literal_prefix(a), _literal_prefix(b))


def write_sets_disjoint(write_set: Sequence[str],
                        others: Mapping[str, Sequence[str]]) -> list[tuple[str, str, str]]:
    """Every overlapping pair as ``(my_path, other_key, other_path)``; empty = disjoint."""
    out: list[tuple[str, str, str]] = []
    for mine in write_set:
        for other_key in sorted(others):
            for other_path in others[other_key]:
                if paths_overlap(mine, other_path):
                    out.append((mine, other_key, other_path))
    return out


# --------------------------------------------------------------------- dry run

def _split_segments(cmd: str) -> list[list[str]]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segments: list[list[str]] = [[]]
    for tok in lex:
        if _SEP_RE.match(tok):
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _executable_found(word: str, repo_root: Path) -> bool:
    if "/" in word:
        p = Path(word)
        if not p.is_absolute():
            p = repo_root / word
        return p.is_file() or (p.exists() and os.access(p, os.X_OK))
    return word in _BUILTINS or shutil.which(word) is not None


def _pytest_args(words: list[str]) -> list[str] | None:
    """Arguments after ``pytest`` / ``python -m pytest``; None if not a pytest call."""
    base = words[0].rsplit("/", 1)[-1]
    if base in ("pytest", "py.test"):
        return words[1:]
    if _PYTHONS.match(base):
        for i in range(1, len(words) - 1):
            if words[i] == "-m" and words[i + 1] == "pytest":
                return words[i + 2:]
    return None


def default_dry_run(cmd: str, repo_root: Path) -> tuple[bool, str]:
    """Check a command line without running it: executables and test paths must exist."""
    repo_root = Path(repo_root)
    try:
        segments = _split_segments(cmd)
    except ValueError as exc:
        return False, f"cannot parse command: {exc}"
    if not segments:
        return False, "empty command"
    if len(segments) > _SEGMENT_CAP:
        return False, f"too many chained commands (max {_SEGMENT_CAP})"
    for seg in segments:
        words = list(seg)
        while words and _ASSIGN_RE.match(words[0]):
            words.pop(0)
        if not words:
            continue
        if not _executable_found(words[0], repo_root):
            return False, f"executable not found: {words[0]}"
        args = _pytest_args(words)
        if args is None:
            continue
        skip = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg in _PYTEST_VALUE_OPTS:
                skip = True
                continue
            if arg.startswith("-"):
                continue
            path = arg.split("::", 1)[0]
            if ("/" in path or path.endswith(".py")) and not (repo_root / path).exists():
                return False, f"missing test path: {path}"
    return True, "ok"


# ------------------------------------------------------------------- the gate

def _bad_path(entry: object) -> bool:
    if not isinstance(entry, str) or not entry.strip():
        return True
    p = entry.strip().replace("\\", "/")
    if p.startswith(("/", "~")) or re.match(r"[A-Za-z]:", p):
        return True
    return ".." in PurePosixPath(p).parts


def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _read_exists(entry: str, repo_root: Path) -> bool:
    entry = entry.strip()
    if not _is_glob(entry):
        return (repo_root / entry).exists()
    try:
        return next(repo_root.glob(entry), None) is not None
    except (ValueError, NotImplementedError, OSError):
        return False


def check_card(card: Mapping, *, key: str | None = None, repo_root: Path,
               running_write_sets: Mapping[str, Sequence[str]] = _EMPTY,
               leg_max_request: int | None = None,
               dry_run: Callable[[str, Path], tuple[bool, str]] = default_dry_run,
               require_heldout: bool = False) -> Verdict:
    """Gate one card; every failing rule adds a ``Finding`` (order as documented)."""
    repo_root = Path(repo_root)
    found: list[Finding] = []

    def add(code: str, message: str) -> None:
        found.append(Finding(code, message))

    write_set = _as_list(card.get("write_set"))
    read_set = _as_list(card.get("read_set"))
    bad: set[str] = set()
    for field, entries in (("write_set", write_set), ("read_set", read_set),
                           ("forbid_paths", _as_list(card.get("forbid_paths")))):
        for entry in entries:
            if _bad_path(entry):
                bad.add(str(entry))
                add("bad_path", f"{field}: {entry}")

    cmd = card.get("check_cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        add("no_check", "check_cmd is empty")
    else:
        ok, reason = dry_run(cmd, repo_root)
        if not ok:
            add("check_not_runnable", reason)

    for entry in read_set:
        if str(entry) in bad:
            continue
        if not _read_exists(str(entry), repo_root):
            add("read_missing", str(entry))

    others = {k: v for k, v in running_write_sets.items() if k != key}
    good_writes = [str(w) for w in write_set if str(w) not in bad]
    for mine, other_key, other_path in write_sets_disjoint(good_writes, others):
        add("write_overlap", f"{mine} ~ {other_key}:{other_path}")

    if card.get("risk_tags") is None:
        add("no_risk_tags", "risk_tags not set (use [] when reviewed and none)")

    window = card.get("window_class")
    if window not in WINDOW_CLASSES:
        add("no_window_class", "window_class not set")
        window = None

    est = card.get("estimate_tokens")
    if isinstance(est, bool) or not isinstance(est, int):
        add("estimate_missing", "estimate_tokens not set")
    else:
        if window is not None and est > USABLE_FRACTION * WINDOW_TOKENS[window]:
            add("estimate_too_big",
                f"{est} > {USABLE_FRACTION} * {WINDOW_TOKENS[window]} ({window})")
        if leg_max_request is not None and est > leg_max_request:
            add("leg_request_too_big", f"{est} > {leg_max_request}")

    if require_heldout:
        held = card.get("heldout_check")
        if not isinstance(held, str) or not held.strip():
            add("no_heldout", "heldout_check is empty")

    return Verdict(key, tuple(found))


def check_cards(cards: Mapping[str, Mapping], **kw) -> dict[str, Verdict]:
    """Gate many cards (sorted by key); batch peers are not treated as running."""
    return {k: check_card(cards[k], key=k, **kw) for k in sorted(cards)}
