"""Context pack for inline-only legs and the big-file brief warning (D-3622).

An inline-only leg (``tool_mode`` "inline-only", e.g. mimo-v2.6-pro) cannot read files, so
``card["context"]`` lists the slices it needs: ``{"path", "start", "end"}`` (1-based, inclusive) or
``"path:start-end"``.  ``pack_prompt`` appends them under a CONTEXT PACK section, each fenced and
labelled ``path:start-end``, capped by ``PLANGRAPH_CONTEXT_PACK_MAX_CHARS`` (default 60000); slices
over the cap are dropped and named in one ``omitted:`` line.  Tool legs read files themselves and get
no pack.  ``warnings`` names every repo file the brief mentions that has over 400 lines and no slice.
Paths are relative to the run cwd; a path that leaves it (lexically or by symlink) refuses the spawn.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from plangraph.routing_errors import ParameterRefused

BIG_FILE_LINES = 400
DEFAULT_MAX_CHARS = 60_000
_SLICE = re.compile(r'^(.+):(\d+)-(\d+)$')
_NAME = re.compile(r'(?<![\w./-])((?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/-])')


class ContextError(ParameterRefused):
    """A ``context`` entry is malformed, unreadable or outside the run cwd."""


def _locate(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or '\0' in raw:
        raise ContextError('context path must be a non-empty string')
    path = Path(os.path.normpath(root / raw))
    if not path.is_relative_to(root):
        raise ContextError(f'context path {raw!r} is outside the run cwd')
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ContextError(f'context path {raw!r} is a symlink out of the run cwd')
    if not resolved.is_file():
        raise ContextError(f'context path {raw!r} is not a file in the run cwd')
    return resolved


def _lines(path: Path) -> list[str]:
    return path.read_text('utf-8', errors='replace').splitlines()


def entries(card) -> list[tuple[str, int, int]]:
    """``card["context"]`` as ``(path, start, end)`` tuples; malformed entries refuse."""
    raw = card.get('context') if isinstance(card, dict) else None
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ContextError('context must be a list')
    out = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            m = _SLICE.match(item)
            parts = (m[1], int(m[2]), int(m[3])) if m else None
        elif isinstance(item, dict):
            start, end = item.get('start'), item.get('end')
            parts = (item.get('path'), start, end) if type(start) is int and type(end) is int else None
        else:
            parts = None
        if not parts or not isinstance(parts[0], str) or not 1 <= parts[1] <= parts[2]:
            raise ContextError(f'context[{i}] must be "path:start-end" or {{path, start, end}} with 1 <= start <= end')
        out.append(parts)
    return out


def slices(card, cwd) -> list[tuple[str, int, int, str]]:
    """``(path, start, end, text)`` per entry, in order; end is clamped to the file length."""
    root, out = Path(cwd).resolve(), []
    for raw, start, end in entries(card):
        lines = _lines(_locate(root, raw))
        end = min(end, len(lines))
        out.append((raw, start, end, '\n'.join(lines[start - 1:end])))
    return out


def max_chars() -> int:
    try:
        return int(os.environ.get('PLANGRAPH_CONTEXT_PACK_MAX_CHARS', DEFAULT_MAX_CHARS))
    except ValueError:
        return DEFAULT_MAX_CHARS


def pack_prompt(prompt: str, card, cwd) -> str:
    """The prompt plus a CONTEXT PACK section; unchanged without ``context``."""
    packed = slices(card, cwd)
    if not packed:
        return prompt
    budget, blocks, omitted = max_chars(), [], []
    for raw, start, end, text in packed:
        label = f'{raw}:{start}-{end}'
        block = f'--- {label} ---\n```\n{text}\n```'
        if len(block) > budget:
            omitted.append(label)
            continue
        budget -= len(block)
        blocks.append(block)
    section = '\n\n=== CONTEXT PACK ===\n' + '\n'.join(blocks)
    if omitted:
        section += '\nomitted: ' + ', '.join(omitted)
    return prompt + section + '\n=== END CONTEXT PACK ===\n'


def warnings(prompt: str, card, cwd) -> list[str]:
    """One ``big_file_unsliced`` warning per repo file the brief names (over 400 lines, no slice)."""
    root = Path(cwd).resolve()
    covered = {_locate(root, raw) for raw, _, _ in entries(card)}
    found = []
    for name in list(dict.fromkeys(_NAME.findall(prompt[:200_000])))[:200]:
        try:
            path = _locate(root, name)
            count = len(_lines(path))
        except (ContextError, OSError):
            continue
        if count > BIG_FILE_LINES and path not in covered:
            found.append(f'big_file_unsliced: {name} {count} lines; name one function per slice in card.context')
    return found


def annotate(record: dict, prompt: str, card, cwd) -> list[str]:
    """Validate ``context`` (refuses a bad entry) and put the warnings on ``record`` and its events."""
    slices(card, cwd)
    found = warnings(prompt, card, cwd)
    if found:
        record['warnings'] = found
        record.setdefault('events', []).extend({'event': 'big_file_unsliced', 'detail': w} for w in found)
    return found
