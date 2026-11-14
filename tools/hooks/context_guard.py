"""context_guard: a Claude Code PostToolUse hook that flags context-window cap.

Reads hook JSON from stdin and sums input tokens plus both cache buckets in
the last usable assistant usage record in a bounded tail; non-numeric records
are stepped past. context_compaction incrementally scans transcript identity
and compaction boundaries. Every compaction requires handoff even below cap,
including transcripts without fleet identity or a session name.
Stdlib-only and portable; hook mode fails silently, while ready reports errors.
Ready v2 validates a handoff/card pair and persists their hashes before kicking
the watchdog. fill_from_usage and resolve_thresholds are public shared helpers.

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

import hashlib
import json
import os
import shutil
import subprocess
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


def fill_from_usage(usage) -> int | None:
    """Input + both cache buckets; missing fields are zero, non-integers invalid."""
    if not isinstance(usage, dict):
        return None
    total = 0
    for field in _USAGE_FIELDS:
        value = usage.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        total += value
    return total


def resolve_thresholds(handoff=None, hard=None) -> tuple[int, int]:
    """Explicit values, else environment, else defaults (hook mode clamps hard)."""
    return (handoff if handoff is not None else _env_int("AUTOOS_CTX_HANDOFF", DEFAULT_HANDOFF),
            hard if hard is not None else _env_int("AUTOOS_CTX_HARD", DEFAULT_HARD))


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
    return fill_from_usage(usage)


def context_tokens(path) -> int | None:
    """Last usable fill in TAIL_BYTES; drop the initial partial line after seeking."""
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
    """Fail-silent PostToolUse; print before persisting so failed writes re-announce."""
    scanned = None
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
        handoff, hard = resolve_thresholds()
        hard = max(hard, handoff)  # a garbled pair must never hide or misreport the hard cap
        try:
            from context_compaction import scan
        except Exception:
            scan = None
            if os.environ.get("AUTOOS_CTX_DEBUG"):
                print("context_guard: context_compaction unavailable; continuing without scan", file=sys.stderr)
        # Independent cursor survives clearing below-threshold announce state.
        scan_path = state_dir().with_name("ctx-guard-scan") / ("%s.scan" % session_id)
        try:
            previous = json.loads(read_text_capped(scan_path))
        except (OSError, ValueError):
            previous = None
        scanned = None
        try:
            scanned = scan(transcript_path, previous) if scan else None
        except Exception:
            if os.environ.get("AUTOOS_CTX_DEBUG"):
                print("context_guard: context_compaction unavailable; continuing without scan", file=sys.stderr)
        stored = read_state(session_id) or {}
        scan_state = scanned or {}
        boundary = scan_state.get("boundary")
        compact = bool(boundary)
        if ctx is None:
            if not compact:
                return 0
            ctx = 0
        if ctx < handoff and not compact:
            clear_state(session_id)  # back under the line (compaction): the next climb is announced afresh
            return 0
        level = "hard" if ctx >= hard else "handoff"
        threshold = hard if level == "hard" else handoff
        if not (compact and boundary != stored.get("compaction")) and not should_emit(session_id, ctx, level):
            return 0
        line = emit_line(level, ctx, threshold)
        if compact:
            if "HANDOFF-NOW" not in line:
                line = "HANDOFF-NOW " + line
            line = line.replace(" >= %dk" % (threshold // 1000),
                                " (compaction; handoff required)", 1)
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": line}}
        print(json.dumps(out))
        write_state(session_id, {
            "session_id": session_id, "cwd": cwd, "ctx": ctx,
            "ts": time.time(), "level": level,
            "compaction": boundary,
        })
    except Exception:
        if os.environ.get("AUTOOS_CTX_DEBUG"):
            traceback.print_exc(file=sys.stderr)
        return 0
    finally:
        if scanned is not None:
            try:
                scan_path.parent.mkdir(parents=True, exist_ok=True)
                _write_json(scan_path, scanned)
            except Exception:
                if os.environ.get("AUTOOS_CTX_DEBUG"):
                    traceback.print_exc(file=sys.stderr)
    return 0


def cmd_ready(args) -> int:
    """Validate ready <handoff-path> [--reason cap|router|operator|planned], then mark v2."""
    if not args or args[0].startswith("--"):
        print("context_guard: ready needs <handoff-path>", file=sys.stderr)
        return 2
    if len(args) >= 2 and args[1] == "--reason" and (
            len(args) == 2 or args[2].startswith("--")):
        print("context_guard: --reason needs a value", file=sys.stderr)
        return 2
    reason = args[2] if len(args) == 3 and args[1] == "--reason" else "cap"
    if len(args) not in (1, 3) or (len(args) == 3 and args[1] != "--reason"):
        print("context_guard: ready needs <handoff-path>", file=sys.stderr)
        return 2
    if reason not in ("cap", "router", "operator", "planned"):
        print("context_guard: invalid ready reason: %s" % reason, file=sys.stderr)
        return 2
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not safe_id(session_id):
        print("context_guard: ready needs a valid CLAUDE_CODE_SESSION_ID", file=sys.stderr)
        return 2
    try:
        handoff = Path(args[0])
        card = handoff.with_suffix(".card.md")
        for label, path in (("handoff", handoff), ("card", card)):
            if not path.is_file():
                raise OSError("%s file missing: %s" % (label, path))
        raw_handoff, raw_card = handoff.read_bytes(), card.read_bytes()
        command = os.environ.get("AUTOOS_HANDOFF_CHECK") or "handoff-check"
        checker = shutil.which(command, path=os.environ.get("PATH", ""))
        if checker is None:
            raise OSError("handoff-check not found on PATH: %s" % command)
        result = subprocess.run([checker, str(card)], capture_output=True, text=True, timeout=10)
        if result.returncode:
            print("context_guard: handoff-check refused ready (exit %s):\n%s%s"
                  % (result.returncode, result.stdout, result.stderr), file=sys.stderr)
            return 2
        if handoff.read_bytes() != raw_handoff or card.read_bytes() != raw_card:
            raise OSError("handoff or card changed during ready; rerun ready")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("context_guard: ready validation failed: %s" % exc, file=sys.stderr)
        return 2
    try:
        marker = {"v": 2, "session_id": session_id, "handoff": args[0],
                  "card": str(card), "handoff_sha256": hashlib.sha256(raw_handoff).hexdigest(),
                  "card_sha256": hashlib.sha256(raw_card).hexdigest(),
                  "session_name": None, "role": None, "launched_at": None,
                  "ctx_tokens": None, "reason": reason, "agents": None,
                  "ts": time.time(), "cwd": os.getcwd()}
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / ("%s.ready.json" % session_id)
        _write_json(destination, marker)
        try:
            if handoff.read_bytes() != raw_handoff or card.read_bytes() != raw_card:
                raise OSError("handoff or card changed during marker write; rerun ready")
        except OSError:
            destination.unlink(missing_ok=True)
            raise
    except OSError as exc:
        print("context_guard: cannot write ready marker: %s" % exc, file=sys.stderr)
        return 2
    if os.environ.get("AUTOOS_READY_KICK") != "0":
        try:
            subprocess.run(["systemctl", "--user", "start", "--no-block", "ctx-watchdog.service"],
                           capture_output=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            detail = " ".join(str(exc).splitlines())
            print("context_guard: ready kick failed; timer is the backstop: %s" % detail, file=sys.stderr)
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
