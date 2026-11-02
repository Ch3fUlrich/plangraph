"""Counter titles for unnamed sessions (operator 2026-10-31: every session name ends in a counter).

UserPromptSubmit hook. On a session's first prompt, when the transcript has no custom title yet, it prints
`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": "<cwd name>-NNNN <first words>"}}`.
NNNN comes from the per-host counter that routing's bin/fleet-launch also uses
(~/.local/state/routing/session-seq, lock'd). Named sessions (fleet launches, `-n`) are left alone. A per-session
marker under ~/.local/state/claude-hooks/session-title/ is written only after the title is out (or once the session
is known to be titled), so a failed run is retried on the next prompt instead of consuming the session. Fail-safe:
any error in main() exits 0 with no output, so a prompt is never blocked. Interim until plangraph SU-HOOK-FLEET ships this.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_common import SAFE_ID as _SAFE_ID, atomic_write_text, state_home  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows: no fcntl - the msvcrt fallback in _lock() covers it
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX: no msvcrt
    msvcrt = None

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

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


def next_seq():
    path = Path(os.environ.get("ROUTING_STATE_DIR") or state("routing")) / "session-seq"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        _lock(f)
        f.seek(0)
        raw = f.read().strip()
        try:
            n = int(raw or 0) + 1
            if raw and n < 2:
                raise ValueError
        except ValueError:
            # A garbled counter must not restart at 1 (titles would collide):
            # fall back to a time-derived value and count on from there.
            n = int(time.time())
        f.seek(0)
        f.truncate()
        f.write(str(n))
    return n


def title(cwd, prompt, seq):
    words = _WORD.findall(prompt or "")[:6]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(cwd or "session").name)[:30] or "session"
    text = (f"{base}-{seq:04d} " + " ".join(words)).rstrip()
    if len(text) <= 64:
        return text
    head = text[:64]
    # truncate at a word boundary, never mid-word; a single word longer than
    # the cap has no boundary and is cut hard as the only option
    return head.rsplit(" ", 1)[0] if " " in head and text[64] != " " else head


def _mark(marker):
    """Write the per-session marker, atomically, once the session is settled."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker, "")


def main():
    try:
        doc = json.loads(sys.stdin.read())
        sid = doc.get("session_id")
        if not (isinstance(sid, str) and _SAFE_ID.match(sid)):
            return 0
        marker = state("claude-hooks", "session-title", sid)
        if marker.exists():
            return 0
        if has_title(doc.get("transcript_path") or ""):
            _mark(marker)  # already titled: settle the session, no title to emit
            return 0
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "sessionTitle": title(doc.get("cwd"), doc.get("prompt"), next_seq())}}
        print(json.dumps(out))
        _mark(marker)  # only after the title is out: a failed run retries next prompt
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
