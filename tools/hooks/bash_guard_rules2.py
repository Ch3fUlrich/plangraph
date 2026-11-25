"""Scanning primitives for the Bash PreToolUse guard.

``scan(command)`` lexes a command string the way the shell does and returns
every simple command as a list of ``Word`` objects and every heredoc with its
body. One scanner answers both questions, so quote state is one thing: it
persists across newlines, ``$'..'`` escapes are decoded, and ``$( )``,
backticks and ``<<`` inside double quotes are seen - the places the old
per-line scanners lost track.

The scanner descends into every place the shell runs a command list: the top
level, ``( )`` subshells, ``{ }`` groups, compound-keyword bodies, ``case``
clauses, ``$( )``, ``<( )`` and backticks (a child scan of the unescaped
text). What it cannot read to the end - an unterminated quote, an unbalanced
``$(``, nesting past ``_MAX_NEST`` - raises ``ParseError``; the caller fails
closed. Not modelled: variable values, aliases, functions, brace expansion.
"""
import re

_MAX_NEST = 32  # nested ( ), $( ) and backtick levels before the scan gives up
# Reserved words after which the next word is again a command position.
STARTERS = ("!", "{", "if", "then", "elif", "else", "while", "until", "do", "coproc")

_PLAIN_RUN = re.compile(r"[^ \t\n;&|<>()'\"\\`$]+")
_DQ_RUN = re.compile(r'[^"\\`$]+')
_BRACE_RUN = re.compile(r"[^'\"\\`$}]+")
_ANSI_STR = re.compile(r"((?:[^'\\]|\\.)*)'", re.S)
_OP = re.compile(r";;&|;;|;&|&&|\|\||\|&|[;&|]")
_REDIR = re.compile(r"<<<|&>>?|[<>]&|>>|<>|>\||[<>]")
_ANSI = re.compile(r"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|.)", re.S)
_ANSI_ONE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
             "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


class ParseError(Exception):
    """The command cannot be scanned to the end."""


class Word:
    """One shell word after quote removal. `text`: quotes and escapes removed,
    ``$'..'`` decoded, substitutions kept as source text. `plain`: nothing was
    quoted or escaped (only plain words are reserved words). `live`: bit 1 =
    a backtick or ``$(`` in double quotes, bit 2 = one bare; the calling shell
    expands both. `redir`: a redirection target, not an argument."""
    __slots__ = ("text", "plain", "live", "redir")

    def __init__(self, text, plain=True, live=0, redir=False):
        self.text, self.plain, self.live, self.redir = text, plain, live, redir


def lead(words, extra=()):
    """How many leading words are plain reserved words (``if``, ``!``, ...)."""
    k = 0
    while k < len(words) and words[k].plain and (words[k].text in STARTERS or words[k].text in extra):
        k += 1
    return k


def _ansi_c(body):
    """Decode the escapes of a ``$'...'`` string."""
    def one(m):
        e = m.group(1)
        try:
            if e[0] in "xuU" and len(e) > 1:
                return chr(int(e[1:], 16))
            if e[0] in "01234567":
                return chr(int(e, 8) & 0xFF)
        except ValueError:
            pass
        return _ANSI_ONE.get(e, "\\" + e)
    return _ANSI.sub(one, body)


def scan(command):
    """Scan `command`; returns (commands, heredocs): one list of ``Word`` per
    simple command (nested ones included) and a (delimiter, quoted, body)
    tuple per heredoc. Raises ParseError when it cannot be read to the end."""
    commands, heredocs = [], []
    _Scanner(command, commands, heredocs).parse()
    return commands, heredocs


class _Scanner:
    def __init__(self, text, commands, heredocs, nest=0):
        self.s, self.n, self.i = text, len(text), 0
        self.commands, self.heredocs, self.nest = commands, heredocs, nest
        self.pending = []  # heredocs (delimiter, quoted, dash) whose body starts at the next newline
        self.shift = 0     # > 0 inside (( )) and $(( )): `<<` is a shift, not a heredoc
        if nest > _MAX_NEST:
            raise ParseError("nesting deeper than %d levels" % _MAX_NEST)

    def blanks(self):
        s, n, i = self.s, self.n, self.i
        while i < n and (s[i] in " \t" or s.startswith("\\\n", i)):
            i += 2 if s[i] == "\\" else 1
        self.i = i

    # ─── statement level ──────────────────────────────────────────────────

    def parse(self, closer=None):
        """Read commands until the `closer` (")" or "))") or the end of text."""
        s, n = self.s, self.n
        cmd, cases = [], []  # words of the current command; one state per open `case`

        def flush():
            if cmd:
                self.commands.append(cmd[:])
                del cmd[:]

        while True:
            self.blanks()
            i = self.i
            if i >= n:
                if closer:
                    raise ParseError("unterminated ( ) or $( )")
                flush()
                return
            c = s[i]
            if c == "#":
                j = s.find("\n", i)
                self.i = n if j < 0 else j
            elif c == "\n":
                flush()
                self.i += 1
                self.gather()
            elif c in "<>" or s.startswith("&>", i):
                self.redirect(cmd)
            elif c in ";&|":
                m = _OP.match(s, i)
                self.i = m.end()
                flush()
                if m.group() in (";;", ";&", ";;&") and cases and cases[-1] == "body":
                    cases[-1] = "pat"
            elif c == "(":
                if cases and cases[-1] == "pat":
                    self.i += 1  # the optional "(" before a case pattern
                elif not (s.startswith("((", i) and lead(cmd, ("for",)) == len(cmd) and self.arith(i + 2)):
                    flush()
                    self.i += 1
                    self.sub(")")
            elif c == ")":
                flush()
                if closer == "))":
                    if not s.startswith("))", i):
                        raise ParseError("unbalanced arithmetic expression")
                    self.i += 2
                    return
                self.i += 1
                if cases and cases[-1] == "pat":
                    cases[-1] = "body"  # ends a case pattern list
                elif closer == ")":
                    return
                elif closer is None:
                    raise ParseError("unexpected )")
            else:
                w = self.word()
                is_esac = w.plain and w.text == "esac"
                if cases and (cases[-1] == "pat" or (not cmd and is_esac)):
                    if is_esac:
                        cases.pop()
                    continue  # case patterns are not commands
                if w.plain and w.text == "{" and cmd and cmd[0].plain and cmd[0].text in ("function", "coproc"):
                    flush()  # `function f { ...; }`: the group opens a command list
                cmd.append(w)
                if w.plain and w.text == "in":
                    k = lead(cmd)
                    if len(cmd) == k + 3 and cmd[k].plain and cmd[k].text == "case":
                        flush()
                        cases.append("pat")

    def sub(self, closer):
        if self.nest >= _MAX_NEST:
            raise ParseError("nesting deeper than %d levels" % _MAX_NEST)
        self.nest += 1
        self.parse(closer)
        self.nest -= 1

    def gather(self):
        """Read the bodies of the heredocs queued on the line that just ended."""
        s, n = self.s, self.n
        for delim, quoted, dash in self.pending:
            body = []
            while self.i < n:
                j = s.find("\n", self.i)
                line = s[self.i:] if j < 0 else s[self.i:j]
                self.i = n if j < 0 else j + 1
                if (line.lstrip("\t") if dash else line) == delim:
                    break
                body.append(line)
            self.heredocs.append((delim, quoted, "\n".join(body)))
        self.pending = []

    def redirect(self, cmd):
        s, i = self.s, self.i
        if cmd and i and s[i - 1].isdigit() and cmd[-1].plain and cmd[-1].text.isdigit():
            cmd.pop()  # the fd of `2>&1` or `3<<EOF` is not an argument
        if s.startswith(("<(", ">("), i):
            self.i = i + 2
            self.sub(")")
            cmd.append(Word(s[i:self.i], False))  # process substitution: one argument
            return
        if s.startswith("<<", i) and not s.startswith("<<<", i):
            self.i = i + 2
            if self.shift:
                return
            dash = s.startswith("-", self.i)
            self.i += dash
            self.blanks()
            start = self.i
            w = self.word()
            if self.i > start:
                self.pending.append((w.text, not w.plain, dash))
            return
        self.i = _REDIR.match(s, i).end()
        self.blanks()
        start = self.i
        w = self.word()
        if self.i > start:
            w.redir = True
            cmd.append(w)

    # ─── arithmetic ───────────────────────────────────────────────────────

    def arith(self, j):
        """Parse an arithmetic body that starts at j; False when the text is
        really nested subshells (`((a); b)` and `$((a); b)`)."""
        if self.arith_end(j) is None:
            return False
        self.i = j
        self.shift += 1
        self.sub("))")
        self.shift -= 1
        return True

    def arith_end(self, j):
        """Index past the `))` that closes an arithmetic body starting at j,
        or None when the parentheses of the body do not balance."""
        s, n, depth = self.s, self.n, 0
        while j < n:
            c = s[j]
            if c == "\\":
                j += 1
            elif c in "'\"`":
                j = s.find(c, j + 1)
                if j < 0:
                    return None
            elif c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    return j + 2 if s.startswith("))", j) else None
                depth -= 1
            j += 1
        return None

    # ─── words ────────────────────────────────────────────────────────────

    def word(self):
        """Read one word at self.i (an empty one when a metacharacter is there)."""
        s, n = self.s, self.n
        text, plain, live = [], True, 0
        while self.i < n:
            m = _PLAIN_RUN.match(s, self.i)
            if m:
                text.append(m.group())
                self.i = m.end()
            elif s.startswith("\\\n", self.i):
                self.i += 2  # a line continuation vanishes
            elif s[self.i] in " \t\n;&|<>()":
                break
            else:
                quoted, bits = self.part(text)
                plain, live = plain and not quoted, live | bits
        return Word("".join(text), plain, live)

    def part(self, text):
        """Read the quote, escape or expansion at self.i; returns (quoted, live bits)."""
        s, c = self.s, self.s[self.i]
        if c == "\\":
            text.append(s[self.i + 1:self.i + 2] or "\\")
            self.i = min(self.i + 2, self.n)
            return True, 0
        if c == "'":
            j = s.find("'", self.i + 1)
            if j < 0:
                raise ParseError("unterminated single quote")
            text.append(s[self.i + 1:j])
            self.i = j + 1
            return True, 0
        if c == '"':
            return True, self.dquote(text)
        if c == "`":
            self.backtick(text, False)
            return False, 2
        nxt = s[self.i + 1:self.i + 2]  # c is "$"
        if nxt == "(":
            self.dollar(text)
            return False, 2
        if nxt == "{":
            return False, self.brace(text)
        if nxt == "'":
            self.ansi(text)
            return True, 0
        if nxt != '"':
            text.append("$")  # a lone $; before a quote it is dropped: `$"..."` is a double-quoted string
        self.i += 1
        return False, 0

    def brace(self, text):
        """Read `${...}` at self.i: only the first unquoted `}` ends it, so a
        parenthesis inside is plain text. Returns the live bits."""
        s, live = self.s, 0
        text.append("${")
        self.i += 2
        while self.i < self.n:
            m = _BRACE_RUN.match(s, self.i)
            if m:
                text.append(m.group())
                self.i = m.end()
            elif s[self.i] == "}":
                text.append("}")
                self.i += 1
                return live
            elif s.startswith("\\\n", self.i):
                self.i += 2
            else:
                live |= self.part(text)[1]
        raise ParseError("unterminated ${")

    def dquote(self, text):
        """Read a double-quoted string at self.i; returns the live bit."""
        s, n = self.s, self.n
        i, live = self.i + 1, 0
        while True:
            m = _DQ_RUN.match(s, i)
            if m:
                text.append(m.group())
                i = m.end()
            if i >= n:
                raise ParseError("unterminated double quote")
            c = s[i]
            if c == '"':
                self.i = i + 1
                return live
            if c == "\\":
                nxt = s[i + 1:i + 2]
                if nxt != "\n":
                    text.append(nxt if nxt and nxt in '"\\$`' else "\\" + nxt)
                i += 2
            elif c == "`" or s.startswith("$(", i):
                live = 1
                self.i = i
                if c == "`":
                    self.backtick(text, True)
                else:
                    self.dollar(text)
                i = self.i
            elif s.startswith("${", i):
                self.i = i
                live |= self.brace(text) and 1  # substitutions nested in the expansion run too
                i = self.i
            else:
                text.append(c)  # a lone $
                i += 1

    def backtick(self, text, dq):
        """Read a backtick substitution at self.i and scan its unescaped text."""
        s, n = self.s, self.n
        j, buf = self.i + 1, []
        while j < n and s[j] != "`":
            if s[j] == "\\" and j + 1 < n:
                nxt = s[j + 1]
                if nxt != "\n":
                    buf.append(nxt if nxt in ("$`\\\"" if dq else "$`\\") else "\\" + nxt)
                j += 2
            else:
                buf.append(s[j])
                j += 1
        if j >= n:
            raise ParseError("unterminated backtick")
        text.append(s[self.i:j + 1])
        self.i = j + 1
        _Scanner("".join(buf), self.commands, self.heredocs, self.nest + 1).parse()

    def dollar(self, text):
        """Read `$(...)` or `$((...))` at self.i; the source text stays in the word."""
        start = self.i
        if not (self.s.startswith("$((", start) and self.arith(start + 3)):
            self.i = start + 2
            self.sub(")")
        text.append(self.s[start:self.i])

    def ansi(self, text):
        m = _ANSI_STR.match(self.s, self.i + 2)
        if not m:
            raise ParseError("unterminated $'...' string")
        text.append(_ansi_c(m.group(1)))
        self.i = m.end()
