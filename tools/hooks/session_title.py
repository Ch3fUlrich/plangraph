"""Counter titles for unnamed sessions (operator 2026-10-31: every session name ends in a counter).

UserPromptSubmit hook. On a session's first prompt, when the transcript has no custom title yet, it prints
`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": "<cwd name>-NNNN"}}`: the working
directory's name and the counter, nothing else. The prompt is never read, so no prompt text can reach a title.

NNNN comes from the per-host counter that routing's bin/fleet-launch also uses
(~/.local/state/routing/session-seq, lock'd). A missing or empty counter file and a stored 0 both count on to 0001.
Text that is not a whole number below 9999 (garbled, negative, a counter that reached 9999) restarts at a
clock-derived value in 1000..9999 and counts on from there, so a name never carries more than four digits.

Named sessions (fleet launches, `-n`) are left alone. A per-session marker under
~/.local/state/claude-hooks/session-title/ is written only after the title is out (or once the session is known
to be titled), so a failed run is retried on the next prompt instead of consuming the session. Fail-safe: any
error in main() exits 0 with no output, so a prompt is never blocked. Interim until plangraph SU-HOOK-FLEET
ships this.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_common import atomic_write_text, read_stdin_text, safe_id, state_home  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows: no fcntl - the msvcrt fallback in _lock() covers it
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX: no msvcrt
    msvcrt = None

# Only the last 128 KB of the transcript is scanned for `custom-title`: the
# read stays bounded on a tens-of-MB file. Tradeoff: a title recorded before
# that tail window is missed and the session may get a counter title anyway;
# acceptable because the check runs on the session's first prompts, when the
# transcript is still small.
TITLE_TAIL_BYTES = 128 * 1024


def _lock(f):
    """The exclusive counter lock: flock on POSIX, msvcrt.locking on Windows.

    A platform with neither takes no lock: a lost race just skips a counter
    value, which beats the hook dying (fail-safe: any error exits 0).
    """
    if fcntl is not None:
        fcntl.flock(f, fcntl.LOCK_EX)
    elif msvcrt is not None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)


def state(*parts):
    return state_home().joinpath(*parts)


def has_title(transcript):
    """Whether the transcript tail holds a `custom-title` record.

    Whole lines are matched (the marker need not sit in the first characters),
    but only inside the last TITLE_TAIL_BYTES - see the tradeoff there.
    """
    try:
        with open(transcript, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            if size > TITLE_TAIL_BYTES:
                f.seek(size - TITLE_TAIL_BYTES)
                f.readline()  # drop the partial first line
            else:
                f.seek(0)
            data = f.read()
    except OSError:
        return False
    return any('"custom-title"' in line for line in data.decode("utf-8", "replace").split("\n"))


# The counter is a four-digit ring (-NNNN, fleet naming rule): 0..SEQ_MAX-1 count on by one, SEQ_MAX wraps to 1.
SEQ_MAX = 9999
SEQ_RESTART = 1000


def _counts_on(raw):
    return len(raw) <= len(str(SEQ_MAX)) and raw.isascii() and raw.isdigit() and int(raw) <= SEQ_MAX


def _bump(raw, last=0):
    """The counter after `raw`, the stored text: a fresh file starts at 1, 0..9998 count on, 9999 wraps to 1.

    `last` is the mark beside the counter, the value issued last (written on every call).  An empty, garbled,
    negative, fractional or stale 10-digit counter continues from it, so no name repeats before the ring has turned
    once (9999 launches, far longer than any session lives).  Without a mark an empty file starts at 1 and a garbled
    one restarts at a clock-derived value in SEQ_RESTART..SEQ_MAX.
    """
    if raw and _counts_on(raw):
        n = int(raw) % SEQ_MAX + 1
        return n % SEQ_MAX + 1 if n == last else n   # marked but the counter write was lost: count it as issued
    if 1 <= last <= SEQ_MAX:
        return last % SEQ_MAX + 1
    if not raw:
        return 1
    return SEQ_RESTART + int(time.time()) % (SEQ_MAX - SEQ_RESTART + 1)


def next_seq():
    path = Path(os.environ.get("ROUTING_STATE_DIR") or state("routing")) / "session-seq"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8", errors="replace") as f:  # bytes that are not text are garbled, not fatal
        _lock(f)
        f.seek(0)
        raw = f.read().strip()
        mark = path.with_name(path.name + ".last")
        try:
            last = int(mark.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = 0
        n = _bump(raw, last)
        atomic_write_text(mark, str(n))   # before the counter: a crash in between still leaves the mark
        f.seek(0)
        f.truncate()
        f.write(str(n))
    return n


def title(cwd, seq):
    """`<cwd name>-NNNN`: the directory's cleaned name (at most 30 characters) and the counter, nothing else."""
    name = Path(cwd).name if isinstance(cwd, str) and cwd else ""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:30] or "session"
    return f"{base}-{seq:04d}"


def _mark(marker):
    """Write the per-session marker, atomically, once the session is settled."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker, "")


def main():
    try:
        doc = json.loads(read_stdin_text())
        sid = doc.get("session_id")
        if not safe_id(sid):
            return 0
        marker = state("claude-hooks", "session-title", sid)
        if marker.exists():
            return 0
        if has_title(doc.get("transcript_path") or ""):
            _mark(marker)  # already titled: settle the session, no title to emit
            return 0
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "sessionTitle": title(doc.get("cwd"), next_seq())}}
        print(json.dumps(out))
        _mark(marker)  # only after the title is out: a failed run retries next prompt
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
