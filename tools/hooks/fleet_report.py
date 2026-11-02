"""fleet_report: the Claude Code Stop hook that files a session's status line.

The Stop hook JSON arrives on stdin (`session_id`, `transcript_path`). Only the
last 256 KB of the transcript is read (seek to the tail, first partial line
dropped); the LAST assistant record is taken, its `text` content blocks are
joined (`tool_use`/`tool_result` blocks are never copied), and the first line
starting with a fleet status keyword — DONE, MERGE-READY, BLOCKED, FAIL, FAILED,
RECYCLE-READY, STOP — becomes the message. No keyword line, no name, or no
parent to report to: exit 0, no-op.

The session's name and its parent come from `FLEET_SESSION_NAME` or from the
launch records under `$FLEET_LAUNCH_DIR` (else `~/.local/state/routing/launch`).
A match appends exactly ONE JSON line — redacted and truncated to 300 chars —
to `<state>/inbox/<parent>.jsonl` with a single `os.write` under `O_APPEND`, so
two writers can never interleave half a record. `fleet_inject.py` later feeds
those lines back to the parent as `additionalContext`.

Dedupe is a marker per session: `<state>/inbox/.seen/<session_id>` holds the
last turn id that was filed — at most one successful append per turn marker
write; consumers should dedupe on `turn`. The marker is written only after
the append succeeded — a failed append retries on the next Stop instead of
silently losing the line. The design is at-least-once, not exactly-once: the
marker write can fail after a successful append, and the check-append-mark
window is not serialised across concurrent Stops, so a rare duplicate turn
can slip through.

Fail-silent, like every hook here: bad stdin, a garbled transcript, an unsafe
name, an unwritable state dir — exit 0 with no output, because a Stop hook must
never break the agent that called it. Nothing from the environment and nothing
from tool output is copied; the line passes through `redact()` first (this
repository is public; the masking rules are `redact_common.py`, shared with
`tools/autoos_refs_check.py`). Every directory this hook creates is 0o700,
inbox files 0o600. State: `$FLEET_STATE_DIR` else `~/.local/state/fleet`.

The shared helpers — `resolve_name`, `redact`, `state_dir`, `safe_name`
(`hook_common.safe_id`), `ensure_dir`, `atomic_write` — live here so
`fleet_inject.py` can load this module by path with importlib instead of
duplicating them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_common import read_stdin_text, read_text_capped  # noqa: E402,F401  (read_* re-exported for fleet_inject)
from hook_common import safe_id as safe_name  # noqa: E402,F401  (re-exported for fleet_inject)
from redact_common import redact_credentials  # noqa: E402

# Only the last 256 KB of the transcript is read — enough to hold the most
# recent assistant record while bounding the read on a many-megabyte file.
TAIL_BYTES = 256 * 1024

# The status line, after redaction, travels as both `status` and `line`.
MAX_LINE = 300

# First matching line of the assistant text. (?![\w-]) keeps STOPPED, DONEX
# and hyphenated continuations like DONE-whatever out: the keyword must not
# run into more word characters or a dash. (\b would treat "-" as a boundary
# and file "DONE-whatever" as DONE.)
STATUS_KEYWORDS = ("DONE", "MERGE-READY", "BLOCKED", "FAILED", "FAIL", "RECYCLE-READY", "STOP")
STATUS_RE = re.compile(r"^(%s)(?![\w-])" % "|".join(STATUS_KEYWORDS))

# The masking rules live in redact_common.py (shared with tools/autoos_refs_check.py).
# On top of them a 32+ character run of key characters is masked as a last resort,
# after the known shapes so a known prefix is replaced whole instead of being
# chopped in half. A missed token lands in a public repository, while an
# over-eager redaction only costs a readable word.
_LONG_RUN = re.compile(r"[A-Za-z0-9_-]{32,}")


def state_dir() -> Path:
    """The fleet state directory: `$FLEET_STATE_DIR`, else `~/.local/state/fleet`."""
    override = os.environ.get("FLEET_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "fleet"


def launch_dir() -> Path:
    """The launch-record directory: `$FLEET_LAUNCH_DIR`, else `~/.local/state/routing/launch`."""
    override = os.environ.get("FLEET_LAUNCH_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "routing" / "launch"


def resolve_name(session_id):
    """`(name, parent)` of this session.

    `FLEET_SESSION_NAME` names the session directly when set; the launch
    record whose `session_id` matches supplies `name` (when the environment
    does not) and always supplies `parent`. Either half can come back empty —
    the Stop hook needs both, the injector only its own name, so each caller
    decides what missing means to it. Everything unreadable (missing launch
    dir, garbled record) degrades to empty, never to an exception.
    """
    name = os.environ.get("FLEET_SESSION_NAME") or ""
    parent = ""
    if not isinstance(session_id, str):
        return name, parent
    try:
        paths = sorted(launch_dir().glob("*.json"))
    except OSError:
        return name, parent
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("session_id") != session_id:
            continue
        if not name:
            got = record.get("name")
            name = got if isinstance(got, str) else ""
        got = record.get("parent")
        parent = got if isinstance(got, str) else ""
        break
    return name, parent


def redact(s: str) -> str:
    """`[redacted]` in place of every credential in `s`: `redact_credentials`, then long runs.

    Applied to the status line before it is filed and again when the injector
    prints it, so a line filed by an older writer still ships scrubbed.
    """
    if not isinstance(s, str):
        return ""
    return _LONG_RUN.sub("[redacted]", redact_credentials(s, "[redacted]"))


def ensure_dir(path) -> Path:
    """`path` as a 0o700 directory, creating every missing level with that mode.

    Only levels this call actually creates are chmod'ed — an existing
    directory's mode is left as its owner set it. A level a concurrent hook
    wins the race to create is left as that winner made it (the same 0o700),
    never raised as FileExistsError: the directory is all this call wanted.
    """
    p = Path(path)
    missing = []
    while not p.exists():
        missing.append(p)
        p = p.parent
    for directory in reversed(missing):
        try:
            os.mkdir(str(directory), 0o700)
        except FileExistsError:
            # The exists() probe above is not a lock: a concurrent Stop hook
            # can create this level first. It exists now; move on.
            continue
    return Path(path)


def atomic_write(path, data: bytes) -> None:
    """Write `data` to `path` via a temp file + rename, so a reader never sees half a file."""
    path = Path(path)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(tmp), flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            # Buffered write: it takes all of `data` or raises — a length
            # check here is dead code. (os.write on a raw fd can short-write,
            # which is why append_inbox checks there.)
            fh.write(data)
        os.replace(str(tmp), str(path))
    finally:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass


def assistant_tail(path):
    """`(text, turn)` of the LAST assistant record in the transcript tail, else (None, None).

    Reads only the last `TAIL_BYTES`; the byte offset there is mid-line, so the
    first split element is dropped as a partial line (but never when the whole
    file fit inside the tail). `text` joins the record's `text` content blocks —
    `tool_use`/`tool_result` content is never copied — and `turn` is the
    record's `uuid`, falling back to sha1 of `text`.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return None, None
    tail_len = min(size, TAIL_BYTES)
    offset = size - tail_len
    try:
        with p.open("rb") as fh:
            if offset:
                fh.seek(offset)
            data = fh.read(tail_len)
    except OSError:
        return None, None
    lines = data.decode("utf-8", "replace").split("\n")
    if offset:
        lines = lines[1:]
    for line in reversed(lines):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    piece = block.get("text")
                    if isinstance(piece, str):
                        blocks.append(piece)
        elif isinstance(content, str):
            blocks.append(content)
        joined = "\n".join(blocks)
        turn = record.get("uuid")
        if not (isinstance(turn, str) and turn):
            turn = hashlib.sha1(joined.encode("utf-8", "replace")).hexdigest()
        return joined, turn
    return None, None


def status_line(text: str):
    """`(status, line)` of the first keyword line of `text`, else (None, None).

    `status` is the bare keyword; `line` is the whole matching line, redacted
    and truncated to MAX_LINE, so a token the line quotes never travels.
    """
    for raw in text.splitlines():
        line = raw.strip()
        m = STATUS_RE.match(line)
        if m:
            return m.group(1), redact(line)[:MAX_LINE]
    return None, None


def append_inbox(parent: str, record: dict) -> None:
    """Append one JSON line to the parent's inbox: a single O_APPEND `os.write`.

    The whole line plus its newline goes down in one write, so concurrent
    writers (several children reporting to one parent) cannot interleave.
    """
    inbox = ensure_dir(state_dir() / "inbox")
    payload = (json.dumps(record) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(inbox / ("%s.jsonl" % parent)), flags, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("short write")
    finally:
        os.close(fd)


def run_hook() -> int:
    """The Stop path: stdin -> status line -> one inbox append + the seen marker.

    Any failure — bad stdin, a missing/garbled transcript, a session outside
    the fleet, an unsafe name, a duplicate turn, an unwritable state dir —
    exits 0 with no output. The append happens before the marker is written,
    so a failed append is retried on the next Stop instead of lost.
    """
    try:
        payload = json.loads(read_stdin_text())
        if not isinstance(payload, dict):
            return 0
        session_id = payload.get("session_id")
        transcript_path = payload.get("transcript_path")
        if not (safe_name(session_id) and isinstance(transcript_path, str)):
            return 0
        name, parent = resolve_name(session_id)
        if not (safe_name(name) and safe_name(parent)):
            return 0
        text, turn = assistant_tail(transcript_path)
        if text is None:
            return 0
        status, line = status_line(text)
        if status is None:
            return 0
        marker = state_dir() / "inbox" / ".seen" / session_id
        try:
            seen = marker.read_bytes()
        except OSError:
            seen = None
        if seen == turn.encode("utf-8"):
            return 0
        append_inbox(parent, {
            "ts": int(time.time()),
            "from": name,
            "session_id": session_id,
            "turn": turn,
            "status": status,
            "line": line,
        })
        ensure_dir(marker.parent)
        atomic_write(marker, turn.encode("utf-8"))
    except Exception:
        return 0
    return 0


def main() -> int:
    return run_hook()


if __name__ == "__main__":
    sys.exit(main())
