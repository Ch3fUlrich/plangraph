"""Per-session thresholds for the AutoOS context guard (operator, 2026-10-30).

Runs context_guard.py's PostToolUse hook in-process. When
<state>/claude-hooks/ctx-guard-session/<session_id>.json exists, its "handoff"
and "hard" integers replace AUTOOS_CTX_HANDOFF / AUTOOS_CTX_HARD for that one
session, and {"off": true} skips the guard. Every other session runs the guard
unchanged. <state> is $XDG_STATE_HOME, else ~/.local/state (hook_common.state_home, as the guard).
The AUTOOS_CTX_* names and the guard's autoos/ctx-guard state dir are kept on purpose: the live fleet guard and its
per-session override files depend on them, so renaming them belongs to the cutover item G13.
The guard's HANDOFF/HARD line gets the card rule appended (operator 2026-10-31: fixed handoff format).
The thresholds are re-read on every call, layered as Claude Code layers them:
the nearest project .claude/settings.local.json over .claude/settings.json
(cwd and its parents) over the ~/.claude/settings.json env, so a changed cap
reaches sessions that started before the change (a session's own process env
is fixed at its start).
Fail-safe: a missing or garbled override file is ignored; any error exits 0.
"""
import importlib.util
import io
import json
import os
import re
import sys
import time
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from context_guard import HANDOFF_PATH_ENV, SUBAGENT_BACKSTOP
except Exception:  # missing or broken guard: main() retains the fail-silent diagnostic
    HANDOFF_PATH_ENV = SUBAGENT_BACKSTOP = None

try:
    from hook_common import read_stdin_text, read_text_capped, safe_id, state_home, write_json  # noqa: E402
except ImportError:  # a half-installed hooks dir: main() says so and exits 0, as for a missing context_guard.py
    safe_id = state_home = read_stdin_text = read_text_capped = None

GUARD = str(Path(__file__).resolve().parent / 'context_guard.py')
# The guard's threshold line (context_guard.emit_line), the only line the card
# rule belongs to: anything else the guard emits must pass through untouched.
_HANDOFF_LINE = re.compile(r"^(?:HANDOFF-NOW|HARD-CAP) ctx=")
CARD_RULE = (" Card: fleet-handoff skill format (Goal, Keeper, Done, In flight, Planned, Open, Traps, Drill-down;"
             " max 40 lines, next to the handoff as <stem>.card.md); run `handoff-check <card> --log` before ready.")


def add_card_rule(out: str) -> str:
    """The guard's JSON line with CARD_RULE appended to its HANDOFF/HARD line; anything else unchanged."""
    try:
        doc = json.loads(out)
        hso = doc["hookSpecificOutput"]
        context = hso.get("additionalContext")
        if isinstance(context, str) and _HANDOFF_LINE.match(context):
            backstop = SUBAGENT_BACKSTOP
            hso["additionalContext"] = (context[:-len(backstop)] + CARD_RULE + backstop
                                        if context.endswith(backstop) else context + CARD_RULE)
            return json.dumps(doc) + "\n"
    except Exception:
        pass
    return out


def overrides_dir() -> Path:
    return state_home() / "claude-hooks" / "ctx-guard-session"


ATTENDED_TAIL_MAX = 1024 * 1024
_MACHINE_PREFIX = re.compile(
    r"^(?:<\s*/?[a-z_:][\w:.-]*|SYSTEM NOTIFICATION)", re.I)


def human_provenance(record, sources):
    """Unknown provenance cannot prove a launch or operator attendance."""
    origin = record.get("origin")
    return (not record.get("isMeta") and not record.get("isSidechain") and
            record.get("promptSource") in sources and record.get("turnOrigin") == "human" and
            isinstance(origin, dict) and origin.get("kind") == "human")


def human_content(record):
    """Use the same non-machine user content check for launch and attendance."""
    message = record.get("message", {})
    content = message.get("content")
    if isinstance(content, list):
        if not content or any(not isinstance(b, dict) or b.get("type") != "text"
                              or not isinstance(b.get("text"), str) for b in content):
            return False
        content = "\n".join(b["text"] for b in content)
    if message.get("role") != "user" or not isinstance(content, str):
        raise ValueError("invalid user content")
    return bool(content.strip()) and not _MACHINE_PREFIX.match(content.lstrip())


def attended_until(payload, doc):
    """Prove launch identity in a bounded head, then attendance in a bounded tail."""
    try:
        window = (doc or {}).get("attended_window_s", 1800)
        if type(window) is not int or window <= 0:
            return None
        with open(payload["transcript_path"], "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(0)
            head = fh.read(ATTENDED_TAIL_MAX)
            start = max(0, size - ATTENDED_TAIL_MAX)
            fh.seek(max(0, start - 1))
            partial = bool(start and fh.read(1) != b"\n")
            tail = fh.read(ATTENDED_TAIL_MAX)
        if start:
            head = head[:head.rfind(b"\n") + 1]  # only complete head lines
        if partial:
            cut = tail.find(b"\n") + 1
            if not cut:
                return None
            tail, start = tail[cut:], start + cut  # discard the initial partial line
        first, offset = None, 0
        for line in head.splitlines(keepends=True):
            record = json.loads(line)
            if not isinstance(record, dict):
                return None
            if first is None and record.get("type") == "user":
                if (not human_provenance(record, ("typed", "queued", "sdk")) or
                        not human_content(record)):
                    return None
                first = offset
            offset += len(line)
        if first is None:
            return None
        last, offset = None, start
        for line in tail.splitlines(keepends=True):
            position, offset = offset, offset + len(line)
            record = json.loads(line)
            if not isinstance(record, dict):
                return None
            if record.get("type") != "user":
                continue
            if position == first:
                continue  # launch brief never establishes attendance
            if not human_provenance(record, ("typed", "queued")):
                continue
            if not human_content(record):
                continue
            stamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None
            last = stamp.timestamp()
            if last > time.time():
                return None
        now = time.time()
        return last + window if last is not None and 0 <= now - last < window else None
    except Exception:
        return None


def session_attended(text, doc):
    """Publish a private atomic lease; failure must never bypass the guard."""
    tmp = None
    try:
        payload = json.loads(text)
        sid = payload.get("session_id")
        if not safe_id(sid):
            return False
        path = state_home() / "autoos" / "ctx-guard" / (sid + ".attended.json")
        until = attended_until(payload, doc)
        if until is None:
            path.unlink(missing_ok=True)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        with os.fdopen(fd, "w") as fh:  # mkstemp creates mode 0600
            json.dump({"until": until}, fh)
        os.replace(tmp, path)
        return True
    except Exception:
        return False
    finally:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)


def session_override(text: str):
    """The override dict for the hook payload's session, or None."""
    try:
        sid = json.loads(text).get("session_id")
        if not safe_id(sid):
            return None
        doc = json.loads(read_text_capped(overrides_dir() / ("%s.json" % sid)))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


KEYS = ("AUTOOS_CTX_HANDOFF", "AUTOOS_CTX_HARD")


def _env_of(path):
    try:
        env = json.loads(read_text_capped(path)).get("env") or {}
    except Exception:
        return {}
    out = {}
    for key in KEYS:  # each key parsed on its own: one garbled value drops only itself
        if key in env:
            try:
                out[key] = str(int(env[key]))
            except Exception:
                pass
    return out


def settings_thresholds(cwd=None):
    """AUTOOS_CTX_HANDOFF / AUTOOS_CTX_HARD as Claude Code layers them: the nearest project .claude/settings.local.json,
    then .claude/settings.json (cwd and its parents), then ~/.claude/settings.json; integers only."""
    out = _env_of(Path.home() / ".claude" / "settings.json")
    try:
        here = Path(cwd).resolve() if cwd else None
    except Exception:
        here = None
    for d in ([here] + list(here.parents)) if here else []:
        if d == Path.home():
            break
        proj = {**_env_of(d / ".claude" / "settings.json"), **_env_of(d / ".claude" / "settings.local.json")}
        if proj:
            out.update(proj)
            break
    return out


def set_thresholds(args) -> int:
    """--set SESSION_ID HANDOFF HARD: atomically configure one attended session."""
    try:
        if len(args) != 4 or not safe_id(args[1]):
            return 2
        handoff, hard = int(args[2]), int(args[3])
        if handoff <= 0 or hard <= handoff:
            return 2
        directory = overrides_dir()
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / (args[1] + ".json"), {"handoff": handoff, "hard": hard})
        return 0
    except Exception:
        return 2


def main() -> int:
    if sys.argv[1:2] == ["--set"]:
        return set_thresholds(sys.argv[1:])
    if state_home is None:
        print("ctx_guard_session: hook_common.py missing next to this hook; context guard is off", file=sys.stderr)
        return 0
    try:
        text = read_stdin_text()
    except ValueError:  # over the cap: not a payload this hook handles
        return 0
    try:
        cwd = json.loads(text).get("cwd")
    except Exception:
        cwd = None
    os.environ.update(settings_thresholds(cwd))
    doc = session_override(text)
    if doc and doc.get("off") is True:
        return 0
    if session_attended(text, doc):
        return 0
    if doc:
        # Launch-time, session-local private destination, alongside handoff/hard.
        path = doc.get("handoff_path")
        if HANDOFF_PATH_ENV and isinstance(path, str) and path and "\x00" not in path:
            try:
                os.environ[HANDOFF_PATH_ENV] = path
            except (UnicodeEncodeError, ValueError):
                pass  # An unusable destination must not suppress the guard.
        thresholds = {key: doc[key] for key in ("handoff", "hard")
                      if isinstance(doc.get(key), int) and not isinstance(doc[key], bool)}
        if (any(val <= 0 for val in thresholds.values()) or
                ("handoff" in thresholds and "hard" in thresholds and
                 thresholds["hard"] <= thresholds["handoff"])):
            thresholds = {}
        for key, var in (("handoff", "AUTOOS_CTX_HANDOFF"), ("hard", "AUTOOS_CTX_HARD")):
            if key in thresholds:
                os.environ[var] = str(thresholds[key])
    try:
        spec = importlib.util.spec_from_file_location("context_guard", GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
    except Exception:
        print("ctx_guard_session: context_guard.py missing or broken next to "
              "this hook; context guard is off", file=sys.stderr)
        return 0
    sys.stdin = io.StringIO(text)
    buf, real = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        rc = guard.main([])
    finally:
        sys.stdout = real
    sys.stdout.write(add_card_rule(buf.getvalue()))
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
