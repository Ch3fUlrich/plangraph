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
denies (stderr message, exit 2) exactly three shapes:

1. An unquoted heredoc whose body contains a literal backtick or ``$(`` -
   the incident shape above.
2. A `claude` invocation carrying `--bg` (background) or `-p`/`--print`
   (non-interactive) whose argument contains a live backtick or ``$(``,
   double-quoted or bare - the same hazard one layer up: a backgrounded or
   one-shot `claude` call whose prompt string is built by the CALLING shell,
   not by claude, which expands the substitution before claude ever sees the
   text. Single quotes and a backslash make the text literal and are allowed.
3. Shape 1 or 2 nested any number of layers down inside a command string
   passed to a `bash`/`sh`/`zsh`/`dash`/... `-c` invocation or to `eval` - a
   `shell -c` string is parsed and expanded exactly like an outer command, so
   the same two hazards apply at every nesting level.

"Anywhere" is literal: the rules check every simple command, whether it sits
at the top level, in a ``( )`` subshell, a ``{ }`` group, behind `if`/`then`/
`while`/`!`/..., inside ``$( )`` or backticks, or behind a wrapper such as
`env`, `sudo`, `nice`, `timeout`, `systemd-run` or `xargs`. Quote state
persists across newlines, so a quote opened on one line hides nothing on the
next.

Everything else allows silently (exit 0, no output) - a guard that prints on
every ordinary command is noise nobody will read before disabling it.

The guard fails CLOSED on the command: one that cannot be parsed (unbalanced
quotes or ``$(``, absurd nesting), one over 1 MiB, a hook payload over 8 MiB (never read past that), and a `shell -c`/`eval`
string nested more than three levels deep are denied with a message saying
why. A hook payload that is not a Bash call with a string command (not JSON,
not an object, another tool) is not a command and is allowed with one stderr
note: Claude Code builds the payload, the model cannot shape it.

This is a gate for one accident, not a sandbox: it reads the command text and
does not model variables, aliases, functions, ``find -exec``, or a shell fed
through a pipe or a heredoc.

The scanner (``bash_guard_rules2.scan``) is one recursive-descent lexer; the
rules (``bash_guard_rules.check_command``) run over the simple commands it
reports.

Run directly for a smoke check:

    echo '{"tool_name":"Bash","tool_input":{"command":"cat <<EOF\n`id`\nEOF"}}' \
        | python3 tools/hooks/bash_guard.py; echo "exit=$?"
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bash_guard_rules import check_command  # noqa: E402
from hook_common import STDIN_MAX, TooLarge, read_stdin_text  # noqa: E402


# ─── hook entry point ───────────────────────────────────────────────────

def main():
    try:
        payload = json.loads(read_stdin_text())
    except TooLarge:
        sys.stderr.write("bash-guard: DENIED - hook input is larger than %d bytes; the guard fails closed.\n"
                         % STDIN_MAX)
        return 2
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
    except Exception as exc:  # check_command never raises; this is the last net
        reason = ("bash-guard: DENIED - internal error while scanning the command "
                  "(%s); the guard fails closed." % type(exc).__name__)
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
