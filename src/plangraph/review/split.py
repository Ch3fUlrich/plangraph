"""Split a unified diff into review parts by module (stdlib only).

Never truncates: every byte of a non-blank diff lands in exactly one part, so the
parts joined in order are the input byte for byte whenever its files are already
grouped by module (modules keep their first-appearance order; an interleaved diff is
regrouped, never changed). A blank diff has no parts. A file chunk larger than the
cap stays alone in its own (oversized) part. Quoted (``"a/..."``) and non-ASCII paths
are decoded from git's C-style quoting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["DiffPart", "module_of", "split_diff_by_module", "file_part_map",
           "render_part_map"]

# First path segments that hold several crates/packages one level down.
CONTAINERS = frozenset({"rust", "crates", "packages", "apps", "libs", "src"})
_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_QUOTED_HEADER = re.compile(r'^diff --git ("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|.+)$')
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(token: str) -> str:
    """Decode one git C-style quoted path (octal bytes are UTF-8); plain tokens pass."""
    if not (len(token) >= 2 and token[0] == token[-1] == '"'):
        return token
    raw, body, i = bytearray(), token[1:-1], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                octal = re.match(r"[0-7]{1,3}", body[i + 1:]).group()
                raw.append(int(octal, 8) & 0xFF)
                i += 1 + len(octal)
                continue
            raw.append(_ESCAPES.get(nxt, ord(nxt)))
            i += 2
            continue
        raw += ch.encode("utf-8")
        i += 1
    return raw.decode("utf-8", "replace")


def _header_path(line: str) -> str | None:
    """The new-side path of a ``diff --git`` header line, or None if it is not one."""
    if not line.startswith("diff --git "):
        return None
    if '"' in line:
        match = _QUOTED_HEADER.match(line)
        if match:
            path = _unquote(match.group(2))
            return path[2:] if path.startswith("b/") else path
    match = _HEADER.match(line)
    return match.group(2) if match else None


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
    # split on "\n" only: other separators (form feed, U+2028) are content, not lines
    for line in re.findall(r"[^\n]*\n|[^\n]+", diff_text):
        path = _header_path(line.rstrip("\r\n"))
        if path is not None:
            chunks.append((path, lead + [line]))
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
    for module in by_module:  # first-appearance order keeps grouped input byte-exact
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
