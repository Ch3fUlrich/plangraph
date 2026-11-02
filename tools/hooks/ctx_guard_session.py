"""Per-session thresholds for the AutoOS context guard (operator, 2026-10-30).

Runs context_guard.py's PostToolUse hook in-process. When
<state>/claude-hooks/ctx-guard-session/<session_id>.json exists, its "handoff"
and "hard" integers replace AUTOOS_CTX_HANDOFF / AUTOOS_CTX_HARD for that one
session, and {"off": true} skips the guard. Every other session runs the guard
unchanged. <state> is $XDG_STATE_HOME, else ~/.local/state (as the guard).
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
from pathlib import Path

GUARD = str(Path(__file__).resolve().parent / 'context_guard.py')
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
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
            hso["additionalContext"] = context + CARD_RULE
            return json.dumps(doc) + "\n"
    except Exception:
        pass
    return out


def overrides_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "claude-hooks" / "ctx-guard-session"


def session_override(text: str):
    """The override dict for the hook payload's session, or None."""
    try:
        sid = json.loads(text).get("session_id")
        if not (isinstance(sid, str) and _SAFE_ID.match(sid)):
            return None
        doc = json.loads((overrides_dir() / ("%s.json" % sid)).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


KEYS = ("AUTOOS_CTX_HANDOFF", "AUTOOS_CTX_HARD")


def _env_of(path):
    try:
        env = json.loads(Path(path).read_text(encoding="utf-8")).get("env") or {}
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


def main() -> int:
    text = sys.stdin.read()
    try:
        cwd = json.loads(text).get("cwd")
    except Exception:
        cwd = None
    os.environ.update(settings_thresholds(cwd))
    doc = session_override(text)
    if doc and doc.get("off") is True:
        return 0
    if doc:
        for key, var in (("handoff", "AUTOOS_CTX_HANDOFF"), ("hard", "AUTOOS_CTX_HARD")):
            val = doc.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                os.environ[var] = str(val)
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
