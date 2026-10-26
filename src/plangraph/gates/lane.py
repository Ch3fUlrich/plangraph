"""§0.2b size gate and §0.2f base pin over a lane's git range (pure parts).

Size: added + deleted lines of ``git diff --numstat <merge-base> <head>``,
lockfiles / generated / vendored paths excluded, binary files count 1 line
(design invariant I6).  Warn above 200 lines or 8 files, fail above 400 lines
or 15 files, unless the lane declares a design slice (``Plan-Slice:
docs/design/<ITEM>.md#<n>`` commit trailer) whose ``max_lines`` covers it.
Base pin: at most one merge from main inside the lane.
"""
from __future__ import annotations

import fnmatch
import re

__all__ = ["WARN_LINES", "WARN_FILES", "MAX_LINES", "MAX_FILES", "MAX_MAIN_MERGES",
           "excluded", "count_numstat", "parse_slice_trailer", "design_slices", "size_verdict"]

WARN_LINES, WARN_FILES = 200, 8
MAX_LINES, MAX_FILES = 400, 15
MAX_MAIN_MERGES = 1

_EXCLUDED = (
    "*.lock", "*/package-lock.json", "package-lock.json", "*/pnpm-lock.yaml", "pnpm-lock.yaml",
    "*/yarn.lock", "go.sum", "*/go.sum", "vendor/*", "*/vendor/*", "*_pb2.py", "*_pb2_grpc.py",
    "*.min.js", "*.min.css", "*/generated/*", "generated/*", "*.snap", "*/__snapshots__/*",
)
_SLICE_RE = re.compile(r"^\s*(?P<path>[\w./-]+\.md)#(?P<n>\d+)\s*$")
_ROW_RE = re.compile(r"^\|\s*(?P<n>\d+)\s*\|(?P<scope>[^|]*)\|\s*(?P<max>\d+)\s*\|\s*$")


def excluded(path):
    return any(fnmatch.fnmatch(path, pattern) for pattern in _EXCLUDED)


def _numstat_path(raw):
    """Final path of a numstat path field (``a => b`` and ``dir/{a => b}/f`` renames)."""
    if "=>" not in raw:
        return raw
    m = re.match(r"^(?P<pre>.*)\{(?P<old>[^}]*) => (?P<new>[^}]*)\}(?P<post>.*)$", raw)
    if m:
        return (m.group("pre") + m.group("new") + m.group("post")).replace("//", "/")
    return raw.split(" => ", 1)[1]


def count_numstat(text):
    """``(lines, files, counted_paths)`` from ``git diff --numstat`` output."""
    lines = files = 0
    paths = []
    for row in (text or "").splitlines():
        parts = row.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, raw = parts
        path = _numstat_path(raw)
        if excluded(path):
            continue
        if added == "-" or deleted == "-":
            n = 1
        else:
            n = int(added) + int(deleted)
        lines += n
        files += 1
        paths.append(path)
    return lines, files, paths


def parse_slice_trailer(value):
    """``(design_path, n)`` from a ``Plan-Slice`` trailer value, else ``None``."""
    m = _SLICE_RE.match(value or "")
    return (m.group("path"), int(m.group("n"))) if m else None


def design_slices(text):
    """``{n: max_lines}`` from the ``## Slices`` table of a design document."""
    out, inside = {}, False
    for line in (text or "").splitlines():
        if line.startswith("## "):
            inside = line.strip().lower() == "## slices"
            continue
        if inside:
            m = _ROW_RE.match(line)
            if m:
                out[int(m.group("n"))] = int(m.group("max"))
    return out


def size_verdict(lines, files, slice_budget=None):
    """``(level, message)``; level is ``ok``, ``warn`` or ``fail``."""
    if slice_budget is not None:
        if lines > slice_budget:
            return "fail", f"{lines} changed lines exceed the declared design slice budget {slice_budget}"
        return "ok", f"{lines} lines / {files} files inside the declared design slice ({slice_budget})"
    if lines > MAX_LINES or files > MAX_FILES:
        return "fail", (f"{lines} changed lines / {files} files > {MAX_LINES}/{MAX_FILES}: split the "
                        "item into design slices (Plan-Slice trailer) or a smaller lane (D-2739 §0.2b)")
    if lines > WARN_LINES or files > WARN_FILES:
        return "warn", f"{lines} lines / {files} files above the {WARN_LINES}/{WARN_FILES} warning line"
    return "ok", f"{lines} lines / {files} files"
