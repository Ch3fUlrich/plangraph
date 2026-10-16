"""Split a unified diff into review parts by module (stdlib only).

Never truncates: every byte of the diff lands in exactly one part. A file
chunk larger than the cap stays alone in its own (oversized) part.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["DiffPart", "module_of", "split_diff_by_module", "file_part_map",
           "render_part_map"]

# First path segments that hold several crates/packages one level down.
CONTAINERS = frozenset({"rust", "crates", "packages", "apps", "libs", "src"})
_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


@dataclass
class DiffPart:
    """One review part: its module, files (diff order) and diff text."""

    index: int
    module: str
    files: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def size(self) -> int:
        """UTF-8 byte size of the part text."""
        return len(self.text.encode("utf-8"))


def module_of(path: str) -> str:
    """Top-level dir, or container/crate (rust/<crate>, src/<pkg>)."""
    segs = [s for s in path.strip("/").split("/") if s]
    if len(segs) <= 1:
        return "."
    if segs[0] in CONTAINERS and len(segs) >= 3:
        return "%s/%s" % (segs[0], segs[1])
    return segs[0]


def _chunks(diff_text: str) -> list[tuple[str, str]]:
    """(path, text) per file; text before the first header joins it."""
    chunks: list[tuple[str, list[str]]] = []
    lead: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        match = _HEADER.match(line.rstrip("\r\n"))
        if match:
            chunks.append((match.group(2), lead + [line]))
            lead = []
        elif chunks:
            chunks[-1][1].append(line)
        else:
            lead.append(line)
    if lead and not chunks:
        return [("(unparsed)", "".join(lead))]
    return [(path, "".join(lines)) for path, lines in chunks]


def split_diff_by_module(diff_text: str, max_bytes: int) -> list[DiffPart]:
    """Parts grouped by module, each <= max_bytes unless one file exceeds it."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not diff_text or not diff_text.strip():
        return []
    by_module: dict[str, list[tuple[str, str]]] = {}
    for path, text in _chunks(diff_text):
        by_module.setdefault(module_of(path), []).append((path, text))
    parts: list[DiffPart] = []
    for module in sorted(by_module):
        current: DiffPart | None = None
        for path, text in by_module[module]:
            width = len(text.encode("utf-8"))
            if current is not None and current.size + width > max_bytes:
                current = None
            if current is None:
                current = DiffPart(index=len(parts) + 1, module=module)
                parts.append(current)
            current.files.append(path)
            current.text += text
    return parts


def file_part_map(parts: list[DiffPart]) -> dict[str, int]:
    """File path to its 1-based part index."""
    return {path: part.index for part in parts for path in part.files}


def render_part_map(parts: list[DiffPart]) -> str:
    """One 'part N [module]: file, file' line per part."""
    return "\n".join("part %d [%s]: %s" % (p.index, p.module, ", ".join(p.files))
                     for p in parts)
