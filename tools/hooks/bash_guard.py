#!/usr/bin/env python3
"""Claude Code PreToolUse guard for the Bash tool (FLEET-HOOKS, P0).

Incident this closes: a seat prompt was built with an UNQUOTED heredoc
(``<<EOF``) whose prose body happened to contain backticked shell commands.
On a host running sessions in bypass mode (no classifier in front of Bash),
the shell executed that prose as commands the moment the heredoc was read -
including a `netplan apply` nobody asked for. An unquoted heredoc expands
backticks and ``$(...)`` in its body exactly like a double-quoted string
does; a quoted delimiter (``<<'EOF'``, ``<<"EOF"``, ``<<\\EOF``) suppresses
that expansion entirely, which is the fix this guard enforces.

Reads one PreToolUse hook JSON object from stdin (``{"tool_name": "Bash",
"tool_input": {"command": "..."}, ...}``, the shape Claude Code sends) and
denies (stderr message, exit 2) exactly two shapes:

1. An unquoted heredoc whose body contains a literal backtick or ``$(`` -
   the incident shape above.
2. A `claude` invocation carrying `--bg` (background) or `-p`/`--print`
   (non-interactive) whose double-quoted argument contains a backtick or
   ``$(`` - the same hazard one layer up: a backgrounded or one-shot `claude`
   call whose prompt string is built with an unquoted-feeling double-quoted
   argument lets the CALLING shell, not claude, expand the substitution
   before claude ever sees the text.

Everything else allows silently (exit 0, no output) - a guard that prints on
every ordinary command is noise nobody will read before disabling it. A
malformed or unexpected hook payload fails OPEN (exit 0) with one stderr
note: this is a shell gate, not a sandbox, and a bug here must never be the
thing that bricks an interactive session.

This module intentionally re-implements two small, independent scanners
rather than reach for a real shell grammar: one for heredoc operators (see
``_heredoc_operators_in_line``), one for splitting a command into top-level
simple commands to find `claude` invocations (see
``_split_top_level_commands``). Both track quote state and `(`/`$(`/`$((`
nesting with the same small stack idea, kept separate because they answer
different questions and a shared "generic parser" would hide exactly the
one-character details (`<<<` vs `<<`, `((` vs `$((`) that make the
difference between a real hazard and `echo "a << b"`.

Run directly for a smoke check:

    echo '{"tool_name":"Bash","tool_input":{"command":"cat <<EOF\n`id`\nEOF"}}' \
        | python3 tools/hooks/bash_guard.py; echo "exit=$?"
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bash_guard_rules import check_command  # noqa: E402

_MAX_INPUT_BYTES = 1024 * 1024  # 1 MiB


# ─── hook entry point ───────────────────────────────────────────────────

def main():
    try:
        if hasattr(sys.stdin, "buffer"):
            raw_bytes = sys.stdin.buffer.read()
        else:
            raw_bytes = sys.stdin.read().encode("utf-8", errors="replace")
        if len(raw_bytes) > _MAX_INPUT_BYTES:
            sys.stderr.write("bash-guard: input exceeds 1 MiB size limit, allowing\n")
            return 0
        raw = raw_bytes.decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception as exc:
        sys.stderr.write(
            "bash-guard: could not parse hook input as JSON, allowing (%s)\n" % exc)
        return 0
    if not isinstance(payload, dict):
        sys.stderr.write("bash-guard: hook input is not a JSON object, allowing\n")
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        sys.stderr.write("bash-guard: Bash tool_input missing or malformed, allowing\n")
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return 0
    try:
        reason = check_command(command)
    except Exception as exc:
        sys.stderr.write("bash-guard: internal parse error, allowing (%s)\n" % exc)
        return 0
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
