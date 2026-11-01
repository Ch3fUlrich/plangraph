"""Counter titles for unnamed sessions (operator 2026-10-31: every session name ends in a counter).

UserPromptSubmit hook. On a session's first prompt, when the transcript has no custom title yet, it prints
`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": "<cwd name>-NNNN <first words>"}}`.
NNNN comes from the per-host counter that routing's bin/fleet-launch also uses
(~/.local/state/routing/session-seq, flock'd). Named sessions (fleet launches, `-n`) are left alone. A per-session
marker under ~/.local/state/claude-hooks/session-title/ makes it run once. Fail-safe: any error exits 0 with no output,
so a prompt is never blocked. Interim until plangraph SU-HOOK-FLEET ships this.
"""
import fcntl
import json
import os
import re
import sys
from pathlib import Path

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def state(*parts):
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state").joinpath(*parts)


def has_title(transcript):
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            return any('"custom-title"' in line[:40] for line in f)
    except OSError:
        return False


def next_seq():
    path = Path(os.environ.get("ROUTING_STATE_DIR") or state("routing")) / "session-seq"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            n = int(f.read().strip() or 0) + 1
        except ValueError:
            n = 1
        f.seek(0)
        f.truncate()
        f.write(str(n))
    return n


def title(cwd, prompt, seq):
    words = _WORD.findall(prompt or "")[:6]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(cwd or "session").name)[:30] or "session"
    return (f"{base}-{seq:04d} " + " ".join(words))[:64].rstrip()


def main():
    doc = json.loads(sys.stdin.read())
    sid = doc.get("session_id")
    if not (isinstance(sid, str) and _SAFE_ID.match(sid)):
        return 0
    marker = state("claude-hooks", "session-title", sid)
    if marker.exists():
        return 0
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    if has_title(doc.get("transcript_path") or ""):
        return 0
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                  "sessionTitle": title(doc.get("cwd"), doc.get("prompt"), next_seq())}}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
