"""Deny rules for the Bash PreToolUse guard.

Mechanical split of the original single-file ``bash_guard.py``: this
module holds the rule-level scanners and ``check_command`` - the entry
the hook's ``main`` calls. The scanning primitives live in
``bash_guard_rules2``; the hook entry point, ``main`` and stdin/stdout IO
live in ``bash_guard``, which puts this file's directory on ``sys.path``
once so the sibling imports below resolve. Every function is moved from
the original module as of PR #292 commit cc9332e1; the only logic change
since is the ``_is_short_p_cluster`` narrowing noted in its docstring.

Slice note (S01): ``bash_guard_rules2`` ships in the NEXT slice (S02,
PR #298). Until #298 merges, importing this module raises ImportError at
the ``bash_guard_rules2`` import below and the guard is inert - it fails
open (a traceback on stderr, nothing denied) on every input. The import
is kept as is on purpose: without its scanners there are no deny rules
to run.
"""
import re
import sys

from bash_guard_rules2 import (  # noqa: E402
    _find_unquoted_heredoc_with_injection,
    _split_top_level_commands,
    _tokenize_args,
)

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CLAUDE_BG_OR_PRINT_FLAGS = ("--bg", "-p", "--print")
# getopt(3)-style short-flag metadata: a cluster letter listed here takes
# an option-argument and so swallows the rest of the cluster. `-m` is the
# short spelling of claude's `--model`: `-mp` is `-m p`, not `-m -p`.
_CLAUDE_SHORT_FLAGS_WITH_ARG = ("m",)
# bash/sh/zsh/dash short flags that take an option-argument (besides -c,
# which is what this guard hunts for): `-o` takes an opt-name.
_SHELL_FLAGS_WITH_ARG = ("o",)


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


def _skip_wrappers(words):
    """Skip leading wrapper words before looking for claude or shell commands:
    sudo, env, command, nohup, time, exec, nice, setsid, timeout, builtin,
    and environment variable assignments. Stop at the first non-wrapper word."""
    idx = 0
    n = len(words)
    while idx < n:
        w = words[idx][0]
        base = _basename(w)
        if _ENV_ASSIGN_RE.match(w):
            idx += 1
            continue
        if base == "sudo":
            idx += 1
            while idx < n:
                opt = words[idx][0]
                if opt == "--":
                    idx += 1
                    break
                if opt in ("-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U"):
                    idx += 1
                    if idx < n:
                        idx += 1
                    continue
                if opt.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        if base == "env":
            idx += 1
            while idx < n:
                opt = words[idx][0]
                if opt == "--":
                    idx += 1
                    break
                if opt == "-u":
                    idx += 1
                    if idx < n:
                        idx += 1
                    continue
                if opt == "-i" or opt.startswith("-"):
                    idx += 1
                    continue
                if _ENV_ASSIGN_RE.match(opt):
                    idx += 1
                    continue
                break
            continue
        if base == "nice":
            idx += 1
            while idx < n:
                opt = words[idx][0]
                if opt == "--":
                    idx += 1
                    break
                if opt == "-n":
                    idx += 1
                    if idx < n:
                        idx += 1
                    continue
                if opt.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        if base == "timeout":
            idx += 1
            while idx < n:
                opt = words[idx][0]
                if opt == "--":
                    idx += 1
                    break
                if opt in ("-s", "-k", "--signal", "--kill-after"):
                    idx += 1
                    if idx < n:
                        idx += 1
                    continue
                if opt.startswith("-"):
                    idx += 1
                    continue
                break
            if idx < n and not words[idx][0].startswith("-"):
                idx += 1
            continue
        if base in ("command", "nohup", "time", "exec", "setsid", "builtin"):
            idx += 1
            while idx < n and words[idx][0].startswith("-"):
                if words[idx][0] == "--":
                    idx += 1
                    break
                idx += 1
            continue
        break
    return idx


def _find_claude_bg_or_print_with_quoted_injection(command):
    """The matched flag (`--bg`, `-p` or `--print`) of the first `claude`
    invocation that carries both that flag AND a double-quoted argument
    with an unescaped backtick or `$(` in it, else None. Flag and the
    injected argument may appear in either order."""
    for segment in _split_top_level_commands(command):
        words = _tokenize_args(segment)
        idx = _skip_wrappers(words)
        if idx >= len(words):
            continue
        if _basename(words[idx][0]) != "claude":
            continue
        rest = words[idx + 1:]
        flag_hit = None
        for w, _trig in rest:
            hit = _extract_flag_hit(w)
            if hit:
                flag_hit = hit
                break
        if not flag_hit:
            continue
        if any(trig for _w, trig in rest):
            return flag_hit
    return None


def _find_shell_c_injection(command, depth=0):
    """Recursively scan command strings passed to bash/sh/zsh/dash -c.
    Depth cap is 3; deeper nesting is allowed (exit 0) with a stderr note.

    Flag parsing is getopt(3)-style, like the scanners' cluster handling:
    a letter from ``_SHELL_FLAGS_WITH_ARG`` swallows the rest of the
    cluster as its option-argument (``-oc 'x'`` is ``-o c``, no ``-c``),
    and ``-c`` takes the rest of its cluster or the next word as the
    command string (``-cx 'x'`` runs ``x``, not ``'x'``)."""
    for segment in _split_top_level_commands(command):
        words = _tokenize_args(segment)
        idx = _skip_wrappers(words)
        if idx >= len(words):
            continue
        cmd = _basename(words[idx][0])
        if cmd not in ("bash", "sh", "zsh", "dash"):
            continue
        k = idx + 1
        c_string = None
        while k < len(words):
            arg = words[k][0]
            if arg == "--":
                break
            if arg.startswith("-") and not arg.startswith("--"):
                cluster = arg[1:]
                j = 0
                c_hit = False
                consumed_next = False
                while j < len(cluster):
                    letter = cluster[j]
                    rest = cluster[j + 1:]
                    if letter in _SHELL_FLAGS_WITH_ARG:
                        # option-argument: attached remainder or next word
                        if not rest and k + 1 < len(words):
                            k += 2
                            consumed_next = True
                        break
                    if letter == "c":
                        c_hit = True
                        if rest:
                            c_string = rest
                        elif k + 1 < len(words):
                            c_string = words[k + 1][0]
                        break
                    j += 1
                if c_hit:
                    break
                if not consumed_next:
                    k += 1
                continue
            if arg.startswith("--"):
                if arg in ("--rcfile",) and k + 1 < len(words):
                    k += 2
                    continue
                k += 1
                continue
            break
        if c_string is None:
            continue
        inner_depth = depth + 1
        if inner_depth > 3:
            sys.stderr.write("bash-guard: shell -c nesting depth limit exceeded, allowing\n")
            return None
        reason = check_command(c_string, depth=inner_depth)
        if reason is not None:
            return reason
    return None


# ─── deny-rule entry point ──────────────────────────────────────────────

def check_command(command, depth=0):
    """The deny message for `command`, or None to allow."""
    delim = _find_unquoted_heredoc_with_injection(command)
    if delim is not None:
        return (
            "bash-guard: DENIED - unquoted heredoc <<%s whose body contains a "
            "backtick or $(...) command substitution; the shell expands that "
            "before the command even runs (the netplan-apply incident shape). "
            "Quote the delimiter if this is prose, not a command: <<'%s'."
            % (delim, delim)
        )
    flag = _find_claude_bg_or_print_with_quoted_injection(command)
    if flag is not None:
        return (
            "bash-guard: DENIED - claude %s carries a double-quoted argument "
            "containing a backtick or $(...) command substitution; the CALLING "
            "shell expands that before claude ever sees the prompt. Use single "
            "quotes, or pass the prompt from a file, instead." % flag
        )
    reason = _find_shell_c_injection(command, depth=depth)
    if reason is not None:
        return reason
    return None
