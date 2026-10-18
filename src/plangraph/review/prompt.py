"""Assemble tool-less review prompts (stdlib only)."""
from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = [
    "NO_TOOLS_PREAMBLE",
    "load_tail",
    "vector_block",
    "assemble",
    "sha256",
    "split_parts",
    "part_map",
    "assemble_all",
    "assemble_diff",
]

NO_TOOLS_PREAMBLE = (
    "You have NO tools and no repository access. "
    "The full material is inline below. "
    "Answer in plain text; never emit tool calls."
)


def load_tail() -> str:
    """Tail reminder text stored next to this module."""
    return Path(__file__).with_name("tail.md").read_text(encoding="utf-8")


def vector_block(vectors: list[str]) -> str:
    """Numbered V1..Vn lines under an === VECTORS header."""
    if not vectors:
        raise ValueError("vectors must not be empty")
    lines = ["=== VECTORS"]
    lines.extend("V%d. %s" % (i, text) for i, text in enumerate(vectors, 1))
    return "\n".join(lines)


def assemble(vectors: list[str], parts: list[tuple[str, str]],
             *, preamble: bool = True) -> str:
    """Full prompt: preamble, vectors, labelled parts, tail last."""
    if not vectors:
        raise ValueError("vectors must not be empty")
    out = ""
    if preamble:
        out += NO_TOOLS_PREAMBLE + "\n"
    out += vector_block(vectors)
    for label, text in parts:
        out += "\n=== %s\n" % label + text
    out += "\n" + load_tail().rstrip()
    return out


def sha256(prompt: str) -> str:
    """Hex digest of the prompt utf-8 bytes."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of whole lines each <= limit bytes."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        width = len(line.encode("utf-8"))
        if width > limit:
            if buf:
                chunks.append("".join(buf))
                buf = []
                size = 0
            chunks.append(line)
        elif buf and size + width > limit:
            chunks.append("".join(buf))
            buf = [line]
            size = width
        else:
            buf.append(line)
            size += width
    if buf:
        chunks.append("".join(buf))
    return chunks or [text]


def split_parts(files: dict[str, str], limit: int = 60_000) -> list[dict[str, str]]:
    """Pack path->text map into parts of <= limit bytes by whole files."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    parts: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_size = 0
    for path, text in sorted(files.items()):
        size = len(text.encode("utf-8"))
        if size > limit:
            if current:
                parts.append(current)
                current = {}
                current_size = 0
            for i, chunk in enumerate(_split_text(text, limit), 1):
                parts.append({"%s#%d" % (path, i): chunk})
            continue
        if current and current_size + size > limit:
            parts.append(current)
            current = {}
            current_size = 0
        current[path] = text
        current_size += size
    if current:
        parts.append(current)
    return parts


def part_map(parts: list[dict[str, str]]) -> str:
    """One 'part N: path1, path2' line per part."""
    return "\n".join("part %d: %s" % (i, ", ".join(sorted(part)))
                     for i, part in enumerate(parts, 1))


def assemble_all(vectors: list[str], files: dict[str, str],
                 limit: int = 60_000, preamble: bool = True) -> list[str]:
    """One full prompt per split part, each carrying the file-part map."""
    if not vectors:
        raise ValueError("vectors must not be empty")
    parts = split_parts(files, limit)
    if not parts:
        parts = [{}]
    mapping = part_map(parts)
    prompts = []
    for part in parts:
        labelled = [("FILE-PART MAP", mapping)]
        labelled.extend(sorted(part.items()))
        prompts.append(assemble(vectors, labelled, preamble=preamble))
    return prompts


def assemble_diff(vectors: list[str], diff_text: str,
                  max_bytes: int = 60_000, preamble: bool = True) -> list[str]:
    """One prompt per module part of a diff, each carrying the file map.

    Parts come from split.split_diff_by_module: grouped by module, never
    truncated (a file above max_bytes is alone in its own part).
    """
    from . import split

    if not vectors:
        raise ValueError("vectors must not be empty")
    parts = split.split_diff_by_module(diff_text, max_bytes)
    if not parts:
        return assemble_all(vectors, {}, max_bytes, preamble)
    mapping = split.render_part_map(parts)
    return [assemble(vectors, [("FILE-PART MAP", mapping),
                               ("DIFF PART %d [%s]" % (p.index, p.module), p.text)],
                     preamble=preamble) for p in parts]
