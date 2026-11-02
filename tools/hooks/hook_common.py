"""Shared helpers for the hooks in this directory (extraction, plangraph #299).

The session-id-as-file-name check, the XDG state-home resolution and the
atomic temp-file + rename write each appeared in more than one hook; per repo
Principle 1 (extract on second duplication) they live here now. Import it the
way the hooks import siblings: insert this directory on ``sys.path`` first.

Everything here is stdlib-only and portable (Linux + Windows): ``pathlib``
throughout and no ``fcntl`` anywhere.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# A session id becomes a file name: a letter/digit first, then letters, digits, dot, dash, underscore.
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def safe_id(value) -> bool:
    """True when `value` is usable as a file name under the state dir."""
    return isinstance(value, str) and SAFE_ID.match(value) is not None


# Bounds on what a hook reads: a hook payload (a command is capped at 1 MiB, JSON escaping can grow it
# several-fold) and a small state file. Reads stop at the cap plus one byte.
STDIN_MAX = 8 * 1024 * 1024
FILE_MAX = 1024 * 1024


class TooLarge(ValueError):
    """The input is longer than its cap."""


def read_stdin_text(limit: int = STDIN_MAX) -> str:
    """Stdin as text, reading at most `limit` + 1 bytes; raises TooLarge past `limit`."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is not None:
        data = stream.read(limit + 1)
    else:
        data = sys.stdin.read(limit + 1).encode("utf-8", errors="replace")
    if len(data) > limit:
        raise TooLarge("stdin is larger than %d bytes" % limit)
    return data.decode("utf-8", errors="replace")


def read_text_capped(path, limit: int = FILE_MAX) -> str:
    """A small state file as text; TooLarge past `limit` bytes (the rest is never read)."""
    with open(str(path), "rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise TooLarge("%s is larger than %d bytes" % (Path(path).name, limit))
    return data.decode("utf-8", errors="replace")


def state_home() -> Path:
    """The per-user state directory ($XDG_STATE_HOME, else ~/.local/state)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "state"


def atomic_write_text(path, text: str) -> None:
    """Write `text` via a temp file + rename, so a reader never sees half a file."""
    path = Path(path)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


def write_json(path, data: dict) -> None:
    """Write `data` as JSON via a temp file + rename, so a reader never sees half a file."""
    atomic_write_text(path, json.dumps(data))
