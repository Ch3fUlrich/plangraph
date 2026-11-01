"""Scanning primitives for the Bash PreToolUse guard.

Mechanical split of the original single-file ``bash_guard.py``: this
module holds the quote-aware scanners the deny rules in
``bash_guard_rules`` are built on - the heredoc-operator scanner and the
top-level command splitter / argument tokenizer. Every function is moved
verbatim from the original module; no logic changed.
"""

_SEPARATORS = ";&|\n"


# ─── heredoc scanner ────────────────────────────────────────────────────

def _parse_heredoc_word(line, j):
    """Parse the delimiter word for a `<<`/`<<-` operator starting at `j`.

    Returns (delimiter_text, quoted, new_index). `quoted` is True the moment
    ANY part of the word was single-quoted, double-quoted or backslash-
    escaped - bash's own rule for "this heredoc gets no expansion", matching
    <<'EOF'/<<"EOF"/<<\\EOF alike and leaving bare <<EOF as unquoted.
    """
    n = len(line)
    out = []
    quoted = False
    while j < n:
        c = line[j]
        if c == "'":
            quoted = True
            j += 1
            while j < n and line[j] != "'":
                out.append(line[j])
                j += 1
            if j < n:
                j += 1
            continue
        if c == '"':
            quoted = True
            j += 1
            while j < n and line[j] != '"':
                if line[j] == "\\" and j + 1 < n:
                    out.append(line[j + 1])
                    j += 2
                else:
                    out.append(line[j])
                    j += 1
            if j < n:
                j += 1
            continue
        if c == "\\":
            quoted = True
            if j + 1 < n:
                out.append(line[j + 1])
                j += 2
            else:
                j += 1
            continue
        if c in " \t;&|<>\n":
            break
        out.append(c)
        j += 1
    return "".join(out), quoted, j


def _heredoc_operators_in_line(line):
    """Every real `<<`/`<<-` heredoc operator in one physical line, in
    order, as (delimiter, quoted, dash) - skipping occurrences that are not
    heredoc operators at all:

    - inside single or double quotes (`echo "a << b"`)
    - `<<<` here-strings (a different operator, no body to scan)
    - the `<<` left-shift operator inside `$((...))`/`((...))` arithmetic
    - after a `#` comment that starts a shell word

    A heredoc operator INSIDE an unquoted `$(...)` command substitution is
    still a real heredoc (bash reads it there) and is still reported - only
    arithmetic context turns `<<` into something else.
    """
    ops = []
    i, n = 0, len(line)
    in_single = False
    in_double = False
    stack = []  # top-of-stack 2 = needs "))" to close (arith), 1 = needs ")"
    while i < n:
        c = line[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_double = False
                i += 1
                continue
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "#" and (i == 0 or line[i - 1] in " \t;|&(\n"):
            break
        if line[i:i + 3] == "$((":
            stack.append(2)
            i += 3
            continue
        if line[i:i + 2] == "((":
            stack.append(2)
            i += 2
            continue
        if line[i:i + 2] == "$(":
            stack.append(1)
            i += 2
            continue
        if c == "(":
            stack.append(1)
            i += 1
            continue
        if c == ")":
            if stack:
                needs = stack[-1]
                if needs == 2 and line[i:i + 2] == "))":
                    stack.pop()
                    i += 2
                    continue
                stack.pop()
            i += 1
            continue
        if c == "<":
            if line[i:i + 3] == "<<<":
                i += 3
                continue
            if line[i:i + 2] == "<<":
                if stack and stack[-1] == 2:
                    i += 2  # the shift operator inside arithmetic
                    continue
                j = i + 2
                dash = False
                if j < n and line[j] == "-":
                    dash = True
                    j += 1
                while j < n and line[j] in " \t":
                    j += 1
                delim, quoted, j = _parse_heredoc_word(line, j)
                if delim:
                    ops.append((delim, quoted, dash))
                i = j
                continue
            i += 1
            continue
        i += 1
    return ops


def _has_unescaped_heredoc_injection(text):
    """Scan unquoted heredoc body character by character.
    A backslash escapes the next character, so \\` and \\$( are literal.
    Two backslashes (an escaped backslash) followed by a backtick or $(
    leaves the trigger live and is denied."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "`":
            return True
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            return True
        i += 1
    return False


def _find_unquoted_heredoc_with_injection(command):
    """The delimiter of the first UNQUOTED heredoc whose body contains a
    backtick or `$(`, or None. Heredoc bodies are consumed line by line
    starting right after the line that carries the operator, in the order
    the operators appear, and end at the first line matching the delimiter
    (leading tabs stripped first for `<<-`) - text after that line is never
    body, even if the same delimiter text reappears later.
    """
    lines = command.split("\n")
    i, n = 0, len(lines)
    while i < n:
        ops = _heredoc_operators_in_line(lines[i])
        i += 1
        for delim, quoted, dash in ops:
            body = []
            while i < n:
                candidate = lines[i]
                i += 1
                check_line = candidate.lstrip("\t") if dash else candidate
                if check_line == delim:
                    break
                body.append(candidate)
            if quoted:
                continue
            text = "\n".join(body)
            if _has_unescaped_heredoc_injection(text):
                return delim
    return None


# ─── top-level command splitter and argument tokenizer ──────────────────

def _split_top_level_commands(command):
    """Split on `;`, `&`, `|` and newline OUTSIDE quotes and OUTSIDE any
    unquoted `(`/`$(`/`$((` nesting, so a separator inside an unquoted
    command substitution (`$(a; b)`) does not split the command that
    contains it. Quoted separators never split at all - the quote branches
    below never consult `stack`."""
    parts = []
    buf = []
    i, n = 0, len(command)
    in_single = False
    in_double = False
    stack = []
    while i < n:
        c = command[i]
        if in_single:
            buf.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                buf.append(c)
                buf.append(command[i + 1])
                i += 2
                continue
            buf.append(c)
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(command[i + 1])
            i += 2
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            i += 1
            continue
        if not stack and c in _SEPARATORS:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        if command[i:i + 3] == "$((":
            stack.append(2)
            buf.append(command[i:i + 3])
            i += 3
            continue
        if command[i:i + 2] == "((":
            stack.append(2)
            buf.append(command[i:i + 2])
            i += 2
            continue
        if command[i:i + 2] == "$(":
            stack.append(1)
            buf.append(command[i:i + 2])
            i += 2
            continue
        if c == "(":
            stack.append(1)
            buf.append(c)
            i += 1
            continue
        if c == ")":
            if stack:
                needs = stack[-1]
                if needs == 2 and command[i:i + 2] == "))":
                    stack.pop()
                    buf.append(command[i:i + 2])
                    i += 2
                    continue
                stack.pop()
            buf.append(c)
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _tokenize_args(segment):
    r"""Word-split one simple command, quote-aware. Returns a list of
    (word_text, had_unescaped_backtick_or_cmdsub_in_double_quotes) pairs -
    the second element is True only when an UNESCAPED backtick or `$(`
    occurred literally inside a double-quoted part of that word; an escaped
    one (`\\`` / `\$(`) is deliberately excluded, and a single-quoted or
    bare `$VAR` carries no trigger at all."""
    words = []
    i, n = 0, len(segment)
    cur = []
    cur_trigger = False
    started = False
    while i < n:
        c = segment[i]
        if c in " \t\n":
            if started:
                words.append(("".join(cur), cur_trigger))
                cur = []
                cur_trigger = False
                started = False
            i += 1
            continue
        started = True
        if c == "'":
            j = segment.find("'", i + 1)
            if j == -1:
                cur.append(segment[i + 1:])
                i = n
            else:
                cur.append(segment[i + 1:j])
                i = j + 1
            continue
        if c == '"':
            j = i + 1
            trig = False
            while j < n and segment[j] != '"':
                if segment[j] == "\\" and j + 1 < n:
                    if segment[j + 1] in ('"', '\\', '$', '`', '\n'):
                        cur.append(segment[j + 1])
                        j += 2
                        continue
                    cur.append(segment[j])
                    cur.append(segment[j + 1])
                    j += 2
                    continue
                if segment[j] == "`":
                    trig = True
                elif segment[j] == "$" and j + 1 < n and segment[j + 1] == "(":
                    trig = True
                cur.append(segment[j])
                j += 1
            if trig:
                cur_trigger = True
            i = j + 1 if j < n else n
            continue
        if c == "\\" and i + 1 < n:
            cur.append(segment[i + 1])
            i += 2
            continue
        cur.append(c)
        i += 1
    if started:
        words.append(("".join(cur), cur_trigger))
    return words
