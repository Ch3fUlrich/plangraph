"""Context pack for inline-only legs and the big-file brief warning (D-3622).

An inline-only leg (``tool_mode`` "inline-only", e.g. mimo-v2.6-pro) cannot read files, so
``card["context"]`` lists the slices it needs: ``{"path", "start", "end"}`` (1-based, inclusive) or
``"path:start-end"``.  ``pack_prompt`` appends them under a CONTEXT PACK section, each fenced and
labelled ``path:start-end``, capped by ``PLANGRAPH_CONTEXT_PACK_MAX_CHARS`` (default 60000); slices
over the cap are dropped and named in one ``omitted:`` line.  Tool legs read files themselves and get
no pack.  ``warnings`` names every repo file the brief mentions that has over 400 lines and no slice.
Paths are relative to the run cwd; a path that leaves it (lexically or by symlink) refuses the spawn.
A slice of an untracked or secret-looking file is skipped with a ``context_path_refused`` warning, unread.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from plangraph.routing_errors import ParameterRefused

BIG_FILE_LINES = 400
DEFAULT_MAX_CHARS = 60_000
_SLICE = re.compile(r'^(.+):(\d+)-(\d+)$')
_TOKEN = re.compile(r'''[^\s`'"<>()\[\]{},;=]+''')
#: Secret-looking names never packed (matched on every path part, lower-cased).
DENY = ('*.token', '*.dsn', '*.key', '*.pem', 'id_*', '.env*', 'api-keys*.yml', '*secret*', '*credential*')


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


def tracked(root: Path) -> frozenset[str]:
    """Git-tracked paths under ``root`` (one ``git ls-files -z``); empty when git fails."""
    try:
        out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(p for p in out.decode('utf-8', 'replace').split('\0') if p)


def _refusal(rel: str, files: frozenset[str]) -> str | None:
    parts = rel.lower().split('/')
    if parts[0] == '.git' or any(fnmatch.fnmatchcase(x, pat) for x in parts for pat in DENY):
        return 'secret-looking path'
    return None if rel in files else 'not tracked by git'


def slices(card, cwd):
    """``(kept, refused)``: ``(path, start, end, text)`` per readable entry in order (end clamped to the
    file length), and ``context_path_refused`` warnings for entries never read."""
    root, kept, refused = Path(cwd).resolve(), [], []
    files = tracked(root) if entries(card) else frozenset()
    for raw, start, end in entries(card):
        path = _locate(root, raw)
        rel = path.relative_to(root).as_posix()
        why = _refusal(Path(os.path.normpath(raw)).as_posix(), files) or _refusal(rel, files)
        if why:
            refused.append(f'context_path_refused: {raw} ({why})')
            continue
        lines = _lines(path)
        kept.append((raw, start, min(end, len(lines)), '\n'.join(lines[start - 1:min(end, len(lines))])))
    return kept, refused


def max_chars() -> int:
    try:
        return int(os.environ.get('PLANGRAPH_CONTEXT_PACK_MAX_CHARS', DEFAULT_MAX_CHARS))
    except ValueError:
        return DEFAULT_MAX_CHARS


def _assemble(blocks, omitted) -> str:
    text = '\n\n=== CONTEXT PACK ===\n' + '\n'.join(b for _, b in blocks)
    if omitted:
        text += '\nomitted: ' + ', '.join(omitted)
    return text + '\n=== END CONTEXT PACK ===\n'


def pack_prompt(prompt: str, card, cwd) -> str:
    """The prompt plus a CONTEXT PACK section no longer than the cap (framing and ``omitted:`` included)."""
    packed, _ = slices(card, cwd)
    if not packed:
        return prompt
    cap = max_chars()
    blocks = [(f'{raw}:{start}-{end}', f'--- {raw}:{start}-{end} ---\n```\n{text}\n```') for raw, start, end, text in packed]
    kept, omitted = [], []
    for item in blocks:
        if len(_assemble(kept + [item], omitted)) <= cap:
            kept.append(item)
        else:
            omitted.append(item[0])
    while kept and len(_assemble(kept, omitted)) > cap:   # the omitted line may itself force another drop
        omitted.append(kept.pop()[0])
    return prompt + _assemble(kept, omitted) if kept else prompt


def warnings(prompt: str, card, cwd) -> list[str]:
    """``context_path_refused`` warnings, and one ``big_file_unsliced`` per tracked file the brief names."""
    root = Path(cwd).resolve()
    kept, refused = slices(card, cwd)
    covered = {_locate(root, raw) for raw, *_ in kept}
    names = []
    for token in _TOKEN.findall(prompt[:200_000]):
        token = token.removeprefix('./').rstrip('.:')
        if ('/' in token or '.' in token) and token not in names:
            names.append(token)
    files = tracked(root) if names else frozenset()
    big = []
    for name in names[:500]:
        if name not in files or _refusal(name, files):
            continue
        try:
            path = _locate(root, name)
            count = len(_lines(path))
        except (ContextError, OSError):
            continue
        if count > BIG_FILE_LINES and path not in covered:
            big.append(f'big_file_unsliced: {name} {count} lines; name one function per slice in card.context')
    return refused + big


def annotate(record: dict, prompt: str, card, cwd) -> list[str]:
    """Validate ``context`` (refuses a malformed entry) and put the warnings on ``record`` and its events."""
    found = warnings(prompt, card, cwd)
    if found:
        record['warnings'] = found
        record.setdefault('events', []).extend({'event': w.split(':', 1)[0], 'detail': w} for w in found)
    return found
