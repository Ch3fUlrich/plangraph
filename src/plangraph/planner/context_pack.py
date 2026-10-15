"""Minimal context pack for one worker attempt (design 4.4 / 4.5 / 8).

A pack is plain text: the card (without the held-out check), an optional tail of the last
failing check output, then the files of the card's write set followed by its read set,
bounded by a token budget.  A ``check_fail`` retry is the same pack plus ``check_tail``.

Pure: reads files under ``repo_root`` only.  Entries may be files, directories (files at
depth <= 2, no deeper) or globs; every file is resolved and must stay inside the root
(symlink escapes are skipped), must not match ``forbid_paths``, and must be UTF-8 text.

* ``build_pack``: assemble the pack; raises ``PackTooBig`` when a write-set file cannot
  fit (route the card to a bigger window class).
* ``pack_budget``: token budget of a window class (usable fraction minus a reserve).
* ``estimate_tokens``: ceil(len / 4).
"""
from __future__ import annotations

import fnmatch
import math
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from plangraph.keeper.schema import RISK_TAGS, WINDOW_CLASSES

__all__ = ["CHARS_PER_TOKEN", "WINDOW_TOKENS", "PackFile", "Pack", "PackTooBig",
           "estimate_tokens", "pack_budget", "build_pack"]

CHARS_PER_TOKEN = 4
#: window class -> tokens (design numbers).
WINDOW_TOKENS = {"128k": 131072, "256k": 262144, "1m": 1048576}
ENTRY_CAP = 50
TAIL_MIN, TAIL_MAX = 40, 60
_BINARY_SNIFF = 8192
#: files above this many bytes are never read at all (avoids loading huge blobs).
_MAX_READ_BYTES = 16 * 1024 * 1024
_GLOB_CHARS = frozenset("*?[")
_LIST_FIELDS = ("read_set", "write_set", "exports", "forbid_paths", "smoke_checks")


def estimate_tokens(text: str) -> int:
    """Token estimate: ceil(len(text) / CHARS_PER_TOKEN)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class PackTooBig(ValueError):
    """A write-set file does not fit the budget; the caller should use a bigger window."""

    def __init__(self, needed: int, budget: int, path: str = "") -> None:
        self.needed = needed
        self.budget = budget
        self.path = path
        super().__init__(f"pack needs {needed} tokens, budget {budget}"
                         + (f" (at {path})" if path else ""))


@dataclass(frozen=True)
class PackFile:
    """One file included in a pack."""

    path: str
    tokens: int
    truncated: bool
    role: str  # "write" | "read" | "export"


@dataclass(frozen=True)
class Pack:
    """The assembled context: text, its token estimate, included files, skipped entries."""

    text: str
    tokens: int
    files: tuple[PackFile, ...]
    skipped: tuple[tuple[str, str], ...]  # (path, reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tokens": self.tokens,
            "files": [{"path": f.path, "tokens": f.tokens, "truncated": f.truncated,
                       "role": f.role} for f in self.files],
            "skipped": [[p, r] for p, r in self.skipped],
        }


def pack_budget(window_class: str, fraction: float = 0.55,
                reserve_tokens: int = 8000) -> int:
    """Pack budget: ``WINDOW_TOKENS[class] * fraction - reserve_tokens`` (floored at 0)."""
    if window_class not in WINDOW_TOKENS:
        raise ValueError(f"window_class must be one of {WINDOW_CLASSES}")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) \
            or not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    if isinstance(reserve_tokens, bool) or not isinstance(reserve_tokens, int) \
            or reserve_tokens < 0:
        raise ValueError("reserve_tokens must be a non-negative int")
    return max(0, int(WINDOW_TOKENS[window_class] * fraction) - reserve_tokens)


# --------------------------------------------------------------------------- validation

def _check_card(card: Mapping[str, Any]) -> None:
    if not isinstance(card, Mapping):
        raise ValueError("card must be a mapping")
    outcome = card.get("outcome")
    if not isinstance(outcome, str) or not outcome.strip():
        raise ValueError("card.outcome is required (non-empty string)")
    for name in _LIST_FIELDS:
        value = card.get(name)
        if value is None:
            continue
        if not isinstance(value, (list, tuple)) or not all(
                isinstance(v, str) and v for v in value):
            raise ValueError(f"card.{name} must be a list of non-empty strings")
    for name in ("check_cmd", "heldout_check"):
        value = card.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"card.{name} must be a string or null")
    wc = card.get("window_class")
    if wc is not None and wc not in WINDOW_CLASSES:
        raise ValueError(f"card.window_class must be one of {WINDOW_CLASSES} or null")
    tags = card.get("risk_tags")
    if tags is not None:
        if not isinstance(tags, (list, tuple)) or any(t not in RISK_TAGS for t in tags):
            raise ValueError(f"card.risk_tags must be a list drawn from {RISK_TAGS}")


# --------------------------------------------------------------------------- expansion

def _forbidden(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        pat = posixpath.normpath(pat.replace("\\", "/")) if pat.strip("/") else ""
        if not pat:
            continue
        if rel == pat or rel.startswith(pat + "/") or fnmatch.fnmatch(rel, pat):
            return True
    return False


def _expand(entry: str, root: Path) -> tuple[list[Path], list[Path]]:
    """``(kept, omitted)`` files an entry stands for (sorted; ``kept`` capped at
    ``ENTRY_CAP``); a literal path may not exist."""
    if _GLOB_CHARS & set(entry):
        try:
            found = [p for p in root.glob(entry) if p.is_file()]
        except (NotImplementedError, ValueError, OSError):
            return [], []
    else:
        path = root / entry
        if not path.is_dir():
            return [path], []
        found = [p for p in list(path.glob("*")) + list(path.glob("*/*")) if p.is_file()]
    ordered = sorted(set(found), key=lambda p: p.as_posix())
    return ordered[:ENTRY_CAP], ordered[ENTRY_CAP:]


def _collect(entries: list[str], root: Path, forbid: list[str],
             skipped: list[tuple[str, str]]) -> tuple[dict[str, Path], set[str]]:
    """Resolve entries to ``{display path: file}`` plus the set of missing paths."""
    found: dict[str, Path] = {}
    missing: set[str] = set()
    for entry in entries:
        if os.path.isabs(entry) or entry.startswith(("/", "\\")):
            skipped.append((entry, "outside_root"))
            continue
        kept, omitted = _expand(entry, root)
        skipped += [(Path(os.path.relpath(p, root)).as_posix(), "entry_cap")
                    for p in omitted]
        for path in kept:
            rel = Path(os.path.relpath(path, root)).as_posix()
            try:
                real = path.resolve()
                real_rel = real.relative_to(root).as_posix()
            except (ValueError, OSError, RuntimeError):
                skipped.append((rel, "outside_root"))
                continue
            if _forbidden(rel, forbid) or _forbidden(real_rel, forbid):
                skipped.append((rel, "forbidden"))
            elif not real.exists():
                missing.add(rel)
            elif real.is_file():
                found[rel] = real
    return found, missing


def _load(real: Path, max_file_tokens: int) -> tuple[str, int] | str:
    """Return ``(content, truncated_tokens)`` or a skip reason."""
    try:
        if real.stat().st_size > _MAX_READ_BYTES:
            return "too_large"
        data = real.read_bytes()
    except OSError:
        return "missing"
    if b"\0" in data[:_BINARY_SNIFF]:
        return "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    limit = max_file_tokens * CHARS_PER_TOKEN
    if len(text) > limit:
        cut = estimate_tokens(text) - max_file_tokens
        return text[:limit] + f"\n... [truncated {cut} tokens]", cut
    return text, 0


# --------------------------------------------------------------------------- the pack

def _header(card: Mapping[str, Any], key: str | None) -> str:
    lines = [f"# CARD {key}" if key else "# CARD", f"outcome: {card['outcome']}"]
    if card.get("check_cmd"):
        lines.append(f"check_cmd: {card['check_cmd']}")
    if card.get("window_class"):
        lines.append(f"window_class: {card['window_class']}")
    if card.get("risk_tags"):
        lines.append("risk_tags: " + ", ".join(card["risk_tags"]))
    if card.get("exports"):
        lines.append("exports: " + ", ".join(card["exports"]))
    lines += [f"forbid: {p}" for p in card.get("forbid_paths") or []]
    lines += [f"smoke: {s}" for s in card.get("smoke_checks") or []]
    # heldout_check is deliberately never emitted: it is held out from the worker.
    return "\n".join(lines) + "\n"


def build_pack(card: Mapping[str, Any], *, repo_root: Path, key: str | None = None,
               budget_tokens: int, check_tail: str | None = None, tail_lines: int = 50,
               max_file_tokens: int = 20000) -> Pack:
    """Build the context pack for ``card``; deterministic for equal inputs and files."""
    _check_card(card)
    for name, val in (("budget_tokens", budget_tokens), ("tail_lines", tail_lines),
                      ("max_file_tokens", max_file_tokens)):
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            raise ValueError(f"{name} must be a positive int")
    if check_tail is not None and not isinstance(check_tail, str):
        raise ValueError("check_tail must be a string or None")
    if not Path(repo_root).is_dir():
        raise ValueError("repo_root must be an existing directory")
    root = Path(repo_root).resolve()

    skipped: list[tuple[str, str]] = []
    forbid = list(card.get("forbid_paths") or [])
    writes, new_writes = _collect(list(card.get("write_set") or []), root, forbid, skipped)
    reads, read_missing = _collect(list(card.get("read_set") or []), root, forbid, skipped)
    skipped += [(rel, "missing") for rel in sorted(read_missing)]
    for rel in [r for r in reads if r in writes or r in new_writes]:
        del reads[rel]  # a file in both sets is shown once, as write

    parts = [_header(card, key)]
    if check_tail is not None:
        n = min(max(tail_lines, TAIL_MIN), TAIL_MAX)
        tail = check_tail.splitlines()[-n:]
        parts.append(f"# CHECK OUTPUT (last {len(tail)} lines)\n" + "\n".join(tail) + "\n")

    files: list[PackFile] = []
    cur = "\n".join(parts)
    over = False
    todo = [(rel, "write") for rel in sorted(set(writes) | new_writes)]
    todo += [(rel, "read") for rel in sorted(reads)]
    for rel, role in todo:
        if role == "read" and over:
            skipped.append((rel, "over_budget"))
            continue
        if rel in new_writes:
            block, truncated = f"# FILE {rel} (write, new)\n", False
        else:
            loaded = _load((writes if role == "write" else reads)[rel], max_file_tokens)
            if loaded == "too_large" and role == "write":
                size = (writes[rel]).stat().st_size
                raise PackTooBig(size // CHARS_PER_TOKEN, budget_tokens, rel)
            if isinstance(loaded, str):
                skipped.append((rel, loaded))
                continue
            content, cut = loaded
            truncated = cut > 0
            block = f"# FILE {rel} ({role})" + "\n```\n" + content + "\n```\n"
        need = estimate_tokens(cur + "\n" + block)
        if need > budget_tokens:
            if role == "write":
                raise PackTooBig(need, budget_tokens, rel)
            over = True
            skipped.append((rel, "over_budget"))
            continue
        cur += "\n" + block
        files.append(PackFile(rel, estimate_tokens(block), truncated, role))
    return Pack(cur, estimate_tokens(cur), tuple(files), tuple(skipped))
