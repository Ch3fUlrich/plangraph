"""context_guard: a Claude Code PostToolUse hook that flags context-window cap.

The hook reads the hook JSON off stdin (`transcript_path`, `session_id`, `cwd`)
and reports how full the model's context window is by summing the three fields
that occupy it — `input_tokens` plus both cache buckets — of the *last usable*
assistant usage record near the end of the transcript (a record with non-numeric
usage is stepped past). When that fill crosses
the handoff threshold it prints one `additionalContext` line telling the agent
to write its handoff and run `ready`; past the hard threshold it says stop. It
also drops a per-session state file so the same fill is not re-announced.

The transcript can be tens of megabytes, so only the tail is read: seek to
`size - TAIL_BYTES`, drop the first partial line, and scan the bounded tail from
the end for the last assistant+usage record. Files are opened through
`Path.open` and read in bounded chunks, never iterated whole.

Everything is stdlib-only and portable (Linux + Windows) via `pathlib`. The hook
mode is deliberately fail-silent — a broken transcript, bad stdin or an
unwritable state dir exits 0 with no output, because a PostToolUse hook must
never break the agent that called it. The `ready` and `status` sub-commands are
the exception: those report errors on stderr and exit non-zero.

Thresholds come from the environment: `AUTOOS_CTX_HANDOFF` (default 270000) and
`AUTOOS_CTX_HARD` (default 300000); a non-integer or empty value falls back,
and `AUTOOS_CTX_HARD` is clamped to at least `AUTOOS_CTX_HANDOFF` so a garbled
pair can never make the hard cap unreachable or misreport its threshold.
State lives under `$XDG_STATE_HOME/autoos/ctx-guard/`, else
`~/.local/state/autoos/ctx-guard/`. Set `AUTOOS_CTX_DEBUG` to get the
otherwise-swallowed failure traceback on stderr. The `AUTOOS_CTX_*` names and
the `autoos/ctx-guard` state dir are kept on purpose: the live fleet guard and
its per-session override files depend on them, so renaming them belongs to the
cutover item G13.

The safe-id check, the state-home resolution and the atomic write come from
the shared `hook_common` helper (extraction, plangraph #299).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_common import read_stdin_text, read_text_capped, safe_id, state_home, write_json as _write_json  # noqa: E402

# Only the last 256 KB of the transcript is read — enough to hold the most
# recent assistant record while bounding the read on a many-megabyte file.
TAIL_BYTES = 256 * 1024

# The three usage fields that together fill the context window.
_USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens")

DEFAULT_HANDOFF = 270000
DEFAULT_HARD = 300000
HANDOFF_PATH_ENV = "AUTOOS_CTX_HANDOFF_PATH"
SUBAGENT_BACKSTOP = " If you are a subagent, finish your task and report; this does not apply to you."

# Re-announce only once the fill has grown this much past the last emitted one,
# unless the level itself escalates.
REEMIT_DELTA = 10000

# Sentinel for a tail line that is not the assistant+usage record we want.
_SKIP = object()
_UNKNOWN = object()  # A mask is unknown, not a reason to reuse stale usage.


# A session id becomes a file name: checked by hook_common.safe_id (shared).


def state_dir() -> Path:
    """The per-user state directory for the guard (XDG state home, else home)."""
    return state_home() / "autoos" / "ctx-guard"


def _env_int(name: str, default: int) -> int:
    """An integer threshold from the environment, `default` if missing/garbled."""
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _tail_ctx(line):
    """The context fill of one tail line, `_SKIP`, or None (a non-numeric usage).

    `_SKIP` means "not the record we are looking for" and the scan continues; a
    returned int is the fill, and None marks a record whose usage fields are not
    all integers — the scan steps past it and keeps looking at older records.
    """
    text = line.strip()
    if not text:
        return _SKIP
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return _SKIP
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return _SKIP
    message = obj.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return _SKIP
    if any(usage.get(field) == '[REDACTED]' for field in _USAGE_FIELDS):
        return _UNKNOWN
    total = 0
    for field in _USAGE_FIELDS:
        value = usage.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        total += value
    return total


def context_tokens(path) -> int | None:
    """The context fill of the last usable assistant usage record in the transcript tail.

    Reads only the last `TAIL_BYTES`; the byte offset there is mid-line, so the
    first split element is dropped as a partial line (but never when the whole
    file fit inside the tail). Returns the summed fill, or None when the file is
    missing/unreadable or when no assistant record with all-numeric usage is in
    the tail (a non-numeric record is stepped past, not the last word).
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return None
    tail_len = min(size, TAIL_BYTES)
    offset = size - tail_len
    try:
        with p.open("rb") as fh:
            if offset:
                fh.seek(offset)
            data = fh.read(tail_len)
    except OSError:
        return None
    lines = data.decode("utf-8", "replace").split("\n")
    if offset:
        lines = lines[1:]
    for line in reversed(lines):
        result = _tail_ctx(line)
        if result is _SKIP or result is None:
            continue
        return result
    return None


def read_state(session_id: str):
    """The stored state dict for a session, or None (missing/garbled)."""
    path = state_dir() / ("%s.json" % session_id)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def should_emit(session_id: str, ctx: int, level: str) -> bool:
    """Whether to announce this fill: new/garbled state, handoff->hard, +10k growth, or a fill drop.

    An escalation is judged before the growth delta, so crossing the hard cap is
    announced even when the fill barely moved since the last handoff.
    """
    stored = read_state(session_id)
    stored_ctx = stored.get("ctx") if stored else None
    if not isinstance(stored_ctx, int) or isinstance(stored_ctx, bool):
        return True  # no state, or a garbled one: announce
    if stored.get("level") == "handoff" and level == "hard":
        return True
    # growth since the last announcement; a fill that fell (auto-compact) re-arms the guard
    return ctx >= stored_ctx + REEMIT_DELTA or ctx < stored_ctx


def clear_state(session_id: str) -> None:
    """Drop the session's announce state; a missing file is fine."""
    try:
        (state_dir() / ("%s.json" % session_id)).unlink()
    except OSError:
        pass


def write_state(session_id: str, state: dict) -> None:
    """Persist one session's state file (raises on an unwritable state dir)."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / ("%s.json" % session_id), state)


def emit_line(level: str, ctx: int, threshold: int) -> str:
    """The single-line `additionalContext` names `ready` without paths.

    No newline is embedded: the whole message is one line. Do not expose the
    interpreter or checkout path: neither belongs in the handoff guidance.
    """
    n = ctx // 1000
    h = threshold // 1000
    tail = "then run ready (and SendMessage RECYCLE-READY <path> to your parent if your brief says so)"
    path = os.environ.get(HANDOFF_PATH_ENV) or "your private handoff path"
    marker = "HARD-CAP" if level == "hard" else "HANDOFF-NOW"
    return ("%s ctx=%dk >= %dk: at the next natural stop, write your handoff to %s, %s."
            % (marker, n, h, path, tail)) + SUBAGENT_BACKSTOP


def run_hook() -> int:
    """The PostToolUse path: read stdin, compute the fill, maybe announce.

    Any failure — bad stdin, missing fields, a missing or garbled transcript, an
    unwritable state dir — exits 0 with no output; set AUTOOS_CTX_DEBUG to get
    the swallowed traceback on stderr instead. The state file is written only
    after the line is printed, so a failed run is re-announced next time rather
    than lost.
    """
    try:
        payload = json.loads(read_stdin_text())
        if not isinstance(payload, dict):
            return 0
        if isinstance(payload.get("agent_id"), str) and payload["agent_id"]:
            return 0
        transcript_path = payload.get("transcript_path")
        session_id = payload.get("session_id")
        cwd = payload.get("cwd")
        if not (isinstance(transcript_path, str) and safe_id(session_id)
                and isinstance(cwd, str)):
            return 0
        ctx = context_tokens(transcript_path)
        if ctx is None:
            return 0
        handoff = _env_int("AUTOOS_CTX_HANDOFF", DEFAULT_HANDOFF)
        hard = _env_int("AUTOOS_CTX_HARD", DEFAULT_HARD)
        hard = max(hard, handoff)  # a garbled pair must never hide or misreport the hard cap
        if ctx is _UNKNOWN:
            ctx = hard  # Safety surrogate, not a measured context count.
        if ctx < handoff:
            clear_state(session_id)  # back under the line (compaction): the next climb is announced afresh
            return 0
        level = "hard" if ctx >= hard else "handoff"
        threshold = hard if level == "hard" else handoff
        if not should_emit(session_id, ctx, level):
            return 0
        line = emit_line(level, ctx, threshold)
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": line}}
        print(json.dumps(out))
        write_state(session_id, {
            "session_id": session_id, "cwd": cwd, "ctx": ctx,
            "ts": time.time(), "level": level,
        })
    except Exception:
        if os.environ.get("AUTOOS_CTX_DEBUG"):
            traceback.print_exc(file=sys.stderr)
        return 0
    return 0


def cmd_ready(args) -> int:
    """`ready <handoff-path>`: mark a session recycled, keyed by session id.

    The session id comes from `CLAUDE_CODE_SESSION_ID`; a missing id or path is
    a loud error (exit 2), unlike the fail-silent hook.
    """
    if not args:
        print("context_guard: ready needs <handoff-path>", file=sys.stderr)
        return 2
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not safe_id(session_id):
        print("context_guard: ready needs a valid CLAUDE_CODE_SESSION_ID", file=sys.stderr)
        return 2
    marker = {"session_id": session_id, "handoff": args[0],
              "ts": time.time(), "cwd": os.getcwd()}
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / ("%s.ready.json" % session_id), marker)
    except OSError as exc:
        print("context_guard: cannot write ready marker: %s" % exc, file=sys.stderr)
        return 2
    return 0


def cmd_status() -> int:
    """`status`: one line per state file; skip unreadable or garbled ones."""
    try:
        files = sorted(state_dir().glob("*.json"))
    except OSError:
        return 0
    for path in files:
        try:
            data = json.loads(read_text_capped(path))
        except (OSError, ValueError):  # TooLarge is a ValueError
            continue
        if not isinstance(data, dict):
            continue
        sid = str(data.get("session_id") or path.name.split(".")[0])
        if path.name.endswith(".ready.json"):
            print("%s ready handoff=%s" % (sid, data.get("handoff", "")))
        else:
            ctx = data.get("ctx")
            n = ctx // 1000 if isinstance(ctx, int) and not isinstance(ctx, bool) else "?"
            print("%s level=%s ctx=%sk cwd=%s"
                  % (sid, data.get("level", ""), n, data.get("cwd", "")))
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "ready":
        return cmd_ready(argv[1:])
    if argv and argv[0] == "status":
        return cmd_status()
    return run_hook()


if __name__ == "__main__":
    sys.exit(main())
