"""Deny rules for the Bash PreToolUse guard.

This module holds the rules and ``check_command`` - the entry the hook's
``main`` calls. The scanner lives in ``bash_guard_rules2``; the hook entry
point, ``main`` and stdin/stderr IO live in ``bash_guard``, which puts this
file's directory on ``sys.path`` once so the sibling imports resolve.

The rules run over every simple command ``scan`` reports - top level,
subshells, ``{ }`` groups, compound-keyword bodies, ``$( )``, backticks - so
a command is checked wherever it sits. A command word is found past leading
reserved words (``if``, ``then``, ``!`` ...), ``NAME=value`` words and, after
a wrapper (``env``, ``time``, ``nice``, ``nohup``, ``sudo``, ``timeout``,
``stdbuf``, ``setsid``, ``command``, ``exec``, ``systemd-run``, ``xargs`` ...),
at any later word: the options of a wrapper are not modelled, so every word
after one is tried as the command word. The guard fails closed: a command
that cannot be scanned, is over 1 MiB, or nests ``-c``/``eval`` strings past
three levels is denied.
"""
import re

from bash_guard_rules2 import lead, scan

MAX_COMMAND_BYTES = 1024 * 1024  # 1 MiB
_MAX_C_DEPTH = 3  # `shell -c` / `eval` strings nested inside each other

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_CLAUDE_BG_OR_PRINT_FLAGS = ("--bg", "-p", "--print")
# getopt(3)-style short-flag metadata: a cluster letter listed here takes
# an option-argument and so swallows the rest of the cluster. `-m` is the
# short spelling of claude's `--model`: `-mp` is `-m p`, not `-m -p`.
_CLAUDE_SHORT_FLAGS_WITH_ARG = ("m",)
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "ash", "mksh", "rbash"))
# bash long options that take an argument (the others take none).
_SHELL_LONG_FLAGS_WITH_ARG = ("--rcfile", "--init-file")
_WRAPPERS = frozenset((
    "env", "time", "nice", "nohup", "sudo", "timeout", "stdbuf", "setsid",
    "command", "builtin", "exec", "ionice", "systemd-run", "xargs",
))

_HEREDOC_MSG = (
    "bash-guard: DENIED - unquoted heredoc <<%s whose body contains a "
    "backtick or $(...) command substitution; the shell expands that "
    "before the command even runs (the netplan-apply incident shape). "
    "Quote the delimiter if this is prose, not a command: <<'%s'.")
_CLAUDE_MSG = (
    "bash-guard: DENIED - claude %s carries %s argument "
    "containing a backtick or $(...) command substitution; the CALLING "
    "shell expands that before claude ever sees the prompt. Use single "
    "quotes, or pass the prompt from a file, instead.")
_DEPTH_MSG = (
    "bash-guard: DENIED - shell -c / eval strings nested more than %d levels "
    "deep; the guard cannot scan that far and fails closed. Flatten the "
    "command or run a script file." % _MAX_C_DEPTH)
_SIZE_MSG = (
    "bash-guard: DENIED - command is larger than the 1 MiB scan limit; the "
    "guard fails closed. Pass the text from a file.")
_PARSE_MSG = (
    "bash-guard: DENIED - could not parse this command (%s); the guard fails "
    "closed. Simplify the quoting or pass the text from a file.")


# ─── unquoted heredoc scanner ───────────────────────────────────────────

def _has_unescaped_heredoc_injection(text):
    """Scan an unquoted heredoc body character by character.
    A backslash escapes the next character, so \\` and \\$( are literal.
    Two backslashes (an escaped backslash) followed by a backtick or $(
    leaves the trigger live and is denied."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "`" or (c == "$" and text.startswith("(", i + 1)):
            return True
        i += 1
    return False


# ─── claude --bg / -p / --print scanner ────────────────────────────────

def _basename(token):
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _is_short_p_cluster(word):
    """True if word is a single-dash short-flag cluster that carries a
    literal ``-p`` once expanded getopt(3)-style (``-pq`` is ``-p -q``).

    Cluster letters are read left to right. A letter from
    ``_CLAUDE_SHORT_FLAGS_WITH_ARG`` takes an option-argument and
    swallows the rest of the cluster, so ``-mp`` is ``-m p`` (model gets
    the value "p") and carries no ``-p``. A letter that is not a known
    claude short flag is treated as value-less: ``-pz`` still carries
    ``-p`` (over-deny on an unknown flag is harmless), and ``-print`` is
    ``-p -r -i -n -t``, carrying ``-p`` as its first flag."""
    if not word.startswith("-") or word.startswith("--"):
        return False
    cluster = word[1:]
    if not cluster or not cluster.isascii() or not cluster.isalpha():
        return False
    for ch in cluster:
        if ch in _CLAUDE_SHORT_FLAGS_WITH_ARG:
            return False
        if ch == "p":
            return True
    return False


def _extract_flag_hit(word):
    """If word is or contains a claude background/print flag, return the
    canonical flag name, else None."""
    flag_part = word.partition("=")[0]
    if flag_part in _CLAUDE_BG_OR_PRINT_FLAGS:
        return flag_part
    if _is_short_p_cluster(flag_part):
        return "-p"
    return None


def _suffix(pos, extra):
    """For each index k: the first claude background/print flag at or after
    it, and the live bits of every word at or after it (`extra` carries the
    redirection targets, which can hold a prompt too: `claude -p <<< "$(x)"`)."""
    n = len(pos)
    flags, live = [None] * (n + 1), [extra] * (n + 1)
    for k in range(n - 1, -1, -1):
        flags[k] = _extract_flag_hit(pos[k].text) or flags[k + 1]
        live[k] = live[k + 1] | pos[k].live
    return flags, live


# ─── shell -c scanner ─────────────────────────────────────────────────

def _shell_strings(words, k):
    """Candidate command strings of the shell whose arguments start at
    words[k]. Options are read the way bash reads them: cluster letters left
    to right, each `o`/`O` takes the next word, `c` marks command mode, and
    `-`, `--` or the first operand ends the options. The command string is
    the first operand; the word right after the cluster that carries `c` is
    tried too (dash-style shells take that one), so `bash -ce '...'`,
    `bash -c -e '...'`, `bash -O extglob -c '...'` and `sh -c -- '...'` are
    all read."""
    n, out, c_seen = len(words), [], False
    while k < n:
        arg = words[k].text
        if arg in ("-", "--"):
            k += 1
            break
        if arg.startswith("--"):
            k += 2 if arg in _SHELL_LONG_FLAGS_WITH_ARG else 1
        elif arg[:1] in ("-", "+"):
            nxt = k + 1
            for letter in arg[1:]:
                if letter == "c":
                    c_seen = True
                    if nxt < n:
                        out.append(words[nxt].text)
                elif letter in "oO":
                    nxt += 1
            k = nxt
        else:
            break
    if c_seen and k < n:
        out.append(words[k].text)
    return out


# ─── per-command rules ────────────────────────────────────────────────

def _check_simple(cmd, depth):
    pos = [w for w in cmd if not w.redir]
    i = lead(pos)
    while i < len(pos) and _ENV_ASSIGN_RE.match(pos[i].text):
        i += 1
    if i >= len(pos):
        return None
    wrapped = _basename(pos[i].text) in _WRAPPERS
    sfx, evaled = None, False
    for j in (range(i, len(pos)) if wrapped else (i,)):
        word = pos[j]
        name = _basename(word.text)
        if name == "claude" or (word.live and "claude" in word.text
                                and not _ENV_ASSIGN_RE.match(word.text)):
            if sfx is None:
                sfx = _suffix(pos, max([w.live for w in cmd if w.redir], default=0))
            flag, live = sfx[0][j + 1], sfx[1][j + 1]
            if flag and live:
                return _CLAUDE_MSG % (flag, "a double-quoted" if live & 1 else "an unquoted")
        elif name in _SHELLS:
            for string in _shell_strings(pos, j + 1):
                reason = check_command(string, depth + 1)
                if reason:
                    return reason
        elif name == "eval" and not evaled:
            evaled = True
            reason = check_command(" ".join(w.text for w in pos[j + 1:]), depth + 1)
            if reason:
                return reason
    return None


# ─── deny-rule entry point ──────────────────────────────────────────────

def check_command(command, depth=0):
    """The deny message for `command`, or None to allow. Never raises: a
    command that cannot be scanned is denied (fail closed)."""
    if depth > _MAX_C_DEPTH:
        return _DEPTH_MSG
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND_BYTES:
        return _SIZE_MSG
    try:
        commands, heredocs = scan(command)
        for delim, quoted, body in heredocs:
            if not quoted and _has_unescaped_heredoc_injection(body):
                return _HEREDOC_MSG % (delim, delim)
        for cmd in commands:
            reason = _check_simple(cmd, depth)
            if reason:
                return reason
    except Exception as exc:  # ParseError, RecursionError, MemoryError, a bug: all fail closed
        return _PARSE_MSG % (str(exc) or type(exc).__name__)[:80]
    return None
