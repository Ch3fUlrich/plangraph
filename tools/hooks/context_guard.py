"""context_guard: a Claude Code PreToolUse/PostToolUse hook for ready lock and context-window cap.

Reads hook JSON from stdin and sums input tokens plus both cache buckets in
the last usable assistant usage record in a bounded tail; non-numeric records
are stepped past. context_compaction incrementally scans transcript identity
and compaction boundaries. Fleet compaction requires handoff even below cap.
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
import math
import os
import re
import shlex
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

_PY_SAFE_FLAGS = frozenset({"-B", "-E", "-I", "-O", "-OO", "-S", "-s", "-u"})
_SHELL_PUNCT = frozenset("();<>|&")
# Longest wait for the handoff checker in ready; sized for its live remote child-session reads.
HANDOFF_CHECK_TIMEOUT_S = 60
_READY_REASONS = ("cap", "router", "operator", "planned")
_PREFIX_KEYS = ("hook_event_name", "session_id")
_PREFIX_MAX_FIELDS = 256


def _allowed_guard_paths() -> set[str]:
    """Resolved paths accepted for the guard script in retry commands."""
    targets = {os.path.realpath(__file__)}
    try:
        installed = Path.home() / ".local" / "share" / "plangraph" / "hooks" / "context_guard.py"
        targets.add(os.path.realpath(installed))
    except (OSError, RuntimeError, ValueError):
        pass
    return targets


def _is_allowed_python(token: str) -> bool:
    """True only for plain python/python3, /usr/bin/python3, or resolved sys.executable."""
    if token in ("python3", "python", "/usr/bin/python3"):
        return True
    if not sys.executable:
        return False
    try:
        return token in (sys.executable, os.path.realpath(sys.executable))
    except (OSError, ValueError):
        return token == sys.executable


def _is_guard_cmd(payload: dict) -> bool:
    """True only for a single Bash invocation of `context_guard.py ready|status`."""
    if payload.get("tool_name") != "Bash":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    if any(bad in command for bad in ("`", "$(", "${", "\n", "\r")):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or any(tok and set(tok) <= _SHELL_PUNCT for tok in tokens):
        return False
    idx = 0
    if _is_allowed_python(tokens[idx]):
        idx += 1
        while idx < len(tokens) and tokens[idx] in _PY_SAFE_FLAGS:
            idx += 1
    if idx >= len(tokens):
        return False
    script_tok = tokens[idx]
    if not script_tok:
        return False
    try:
        if os.path.isabs(script_tok):
            resolved = os.path.realpath(script_tok)
        else:
            cwd = payload.get("cwd")
            base = cwd if isinstance(cwd, str) and cwd else os.getcwd()
            resolved = os.path.realpath(os.path.join(base, script_tok))
    except (OSError, ValueError):
        return False
    if resolved not in _allowed_guard_paths():
        return False
    rest = tokens[idx + 1:]
    if rest == ["status"]:
        return True
    if not rest or rest[0] != "ready":
        return False
    args = rest[1:]
    if len(args) == 1:
        return bool(args[0]) and not args[0].startswith("-")
    if len(args) == 3:
        return (bool(args[0]) and not args[0].startswith("-")
                and args[1] == "--reason" and args[2] in _READY_REASONS)
    return False


def _format_ready_ts(ts) -> str:
    """Format the ready marker timestamp as UTC ISO-8601 when numeric, else safe text."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts):
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(ts, str) and ts.strip():
        return ts.replace("\r", " ").replace("\n", " ")
    return "unknown"


def _parse_prefix_fields(text: str) -> dict[str, str]:
    """Extract top-level string fields (`hook_event_name`, `session_id`) from a JSON prefix."""
    fields: dict[str, str] = {}
    n = len(text)
    pos = 0
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n or text[pos] != "{":
        return fields
    pos += 1
    decoder = json.JSONDecoder()
    for index in range(_PREFIX_MAX_FIELDS):
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n or text[pos] == "}":
            break
        if index > 0:
            if text[pos] != ",":
                break
            pos += 1
            while pos < n and text[pos].isspace():
                pos += 1
        if pos >= n or text[pos] != '"':
            break
        try:
            key, pos = decoder.raw_decode(text, pos)
            while pos < n and text[pos].isspace():
                pos += 1
            if pos >= n or text[pos] != ":":
                break
            pos += 1
            while pos < n and text[pos].isspace():
                pos += 1
            val, pos = decoder.raw_decode(text, pos)
        except (ValueError, RecursionError):
            break
        if key in _PREFIX_KEYS and isinstance(val, str):
            fields[key] = val
            if len(fields) == len(_PREFIX_KEYS):
                break
    return fields


def strict_guard():
    return os.environ.get("PLANGRAPH_GUARD_FAIL_CLOSED") == "1"


def _read_v2_ready_marker(path: Path, expected_session_id: str | None = None) -> dict | None:
    """Return a validated v2 ready marker dict, or None when missing, corrupt, or mismatched."""
    try:
        marker = json.loads(read_text_capped(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if strict_guard():
            raise
        return None
    if not isinstance(marker, dict) or marker.get("v") != 2:
        if strict_guard():
            raise ValueError("invalid ready marker")
        return None
    sid = marker.get("session_id")
    if (not safe_id(sid) or path.name != ("%s.ready.json" % sid)
            or (expected_session_id is not None and sid != expected_session_id)):
        if strict_guard():
            raise ValueError("ready marker identity mismatch")
        return None
    return marker


def _resolve_ready_marker(session_id) -> dict | None:
    """Resolve the v2 ready marker for `session_id`, CLAUDE_CODE_SESSION_ID, or any v2 marker in state_dir."""
    if safe_id(session_id):
        return _read_v2_ready_marker(state_dir() / ("%s.ready.json" % session_id), session_id)
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if safe_id(env_sid):
        return _read_v2_ready_marker(state_dir() / ("%s.ready.json" % env_sid), env_sid)
    try:
        paths = sorted(state_dir().glob("*.ready.json"))
    except OSError:
        if strict_guard():
            raise
        return None
    for path in paths:
        marker = _read_v2_ready_marker(path)
        if marker is not None:
            return marker
    return None


def _emit_ready_deny(marker: dict) -> int:
    """Emit the PreToolUse deny JSON for an active v2 ready marker."""
    handoff = marker.get("handoff")
    handoff_str = (handoff.replace("\r", " ").replace("\n", " ")
                   if isinstance(handoff, str) and handoff else "unknown")
    reason = ("context_guard: ready was sent at %s (handoff %s); your successor owns the role. "
              "End your turn now with one final line; no further tool calls."
              % (_format_ready_ts(marker.get("ts")), handoff_str))
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "deny",
                                  "permissionDecisionReason": reason}}
    print(json.dumps(out))
    return 0


def deny_on_unusable_stdin(prefix: str) -> int:
    """Fail closed when oversized or unparsable stdin may be a PreToolUse call and a ready marker exists."""
    fields = _parse_prefix_fields(prefix)
    event_name = fields.get("hook_event_name")
    if event_name is not None and event_name != "PreToolUse":
        return 0
    marker = _resolve_ready_marker(fields.get("session_id"))
    if marker is None:
        return 1 if strict_guard() else 0
    return _emit_ready_deny(marker)


def _run_pre_tool_use(payload: dict) -> int:
    """Deny tool calls once this session's v2 ready marker exists, except ready|status retries."""
    if _is_guard_cmd(payload):
        return 0
    marker = _resolve_ready_marker(payload.get("session_id"))
    if marker is None:
        return 0
    return _emit_ready_deny(marker)


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
    """Fail-silent PostToolUse and ready-locked PreToolUse; print before persisting so failed writes re-announce."""
    scanned = None
    try:
        try:
            text = read_stdin_text()
        except ValueError as exc:
            return deny_on_unusable_stdin(getattr(exc, "prefix", ""))
        try:
            payload = json.loads(text)
        except ValueError:
            return deny_on_unusable_stdin(text)
        if not isinstance(payload, dict):
            return deny_on_unusable_stdin(text)
        if payload.get("hook_event_name") == "PreToolUse":
            return _run_pre_tool_use(payload)
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
        try:
            scanned = scan(transcript_path, previous) if scan else None
        except Exception:
            if os.environ.get("AUTOOS_CTX_DEBUG"):
                print("context_guard: context_compaction unavailable; continuing without scan", file=sys.stderr)
        stored = read_state(session_id) or {}
        scan_state = scanned or {}
        boundary = scan_state.get("boundary") if scan_state.get("fleet") else None
        compact = bool(boundary)
        if ctx < handoff and not compact:
            clear_state(session_id)  # back under the line (compaction): the next climb is announced afresh
            return 0
        level = "hard" if ctx >= hard else "handoff"
        threshold = hard if level == "hard" else handoff
        if not (compact and boundary != stored.get("compaction")) and not should_emit(session_id, ctx, level):
            return 0
        line = emit_line(level, ctx, threshold)
        if compact:
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
        return 1 if strict_guard() else 0
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
    if reason not in _READY_REASONS:
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
        try:
            result = subprocess.run([checker, str(card)], capture_output=True, text=True,
                                    timeout=HANDOFF_CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise OSError("handoff-check timed out after %s seconds; ready needs it to finish"
                          % HANDOFF_CHECK_TIMEOUT_S) from None
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
