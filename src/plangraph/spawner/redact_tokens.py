"""Rust worker-core's shared prefixed-token and wrapped-body policy."""
import re
import unicodedata
from typing import NamedTuple


MAX_WRAPPED_BODY = 4096


class TokenSpec(NamedTuple):
    prefix: str
    body: str
    floor: int
    exact: bool


# Mirror rust/worker-core/src/redact_tokens.rs SPECS, including order.
SPECS = (
    TokenSpec(r'x?sk-', r'[A-Za-z0-9_-]', 1, False),
    TokenSpec(r'gh[pousr]_', r'[A-Za-z0-9_-]', 20, False),
    TokenSpec(r'github_pat_', r'[A-Za-z0-9_-]', 20, False),
    TokenSpec(r'xox[abprs]-', r'[A-Za-z0-9_-]', 10, False),
    TokenSpec(r'AKIA', r'[0-9A-Z]', 16, True),
    TokenSpec(r'AIza', r'[0-9A-Za-z_-]', 30, False),
    TokenSpec(r'[sr]k_(?:live|test)_', r'[A-Za-z0-9]', 16, False),
)


def pattern():
    return '|'.join(r'\b' + s.prefix + s.body + '{' + str(s.floor)
                    + (r'}\b' if s.exact else ',}') for s in SPECS)


TOKENS = re.compile(pattern())
JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')
_PREFIX = re.compile(r'\b(?:' + '|'.join('(' + s.prefix + ')' for s in SPECS) + ')')
_BODY = tuple(re.compile(s.body + '+') for s in SPECS)


def join_shielded_assignments(text, assignment):
    """P: join untouched values without crossing or eating a credential name."""
    # Escape literal openers before assigning indexes to protected names.
    text = text.replace('\ue000', '\ue000\ue001')
    names = []

    def shield(m):
        names.append(m[1])
        return '\ue000' + str(len(names) - 1) + '\ue001' + m[0][len(m[1]):]

    text = redact_wrapped(assignment.sub(shield, text))
    return re.sub('\ue000([0-9]*)\ue001', lambda m: names[int(m[1])] if m[1] else '\ue000', text)


def redact_wrapped(text):
    out, copied = [], 0
    for start, end in _wrapped_spans(text):
        out.extend((text[copied:start], '[REDACTED]'))
        copied = end
    return ''.join((*out, text[copied:]))


_BEARER = re.compile(r'(?i)\bBearer\s+')


def redact_bearer_wrapped(text):
    """Mask wrapped tokens right after a Bearer word, before the word's own rule runs.

    The Bearer rule takes one whitespace-free word and would leave a wrapped
    continuation behind; this must run first (rust/worker-core redact_wrapped.rs).
    """
    starts = {m.end() for m in _BEARER.finditer(text)}
    out, copied = [], 0
    for start, end in _wrapped_spans(text):
        if start in starts:
            out.extend((text[copied:start], '[REDACTED]'))
            copied = end
    return ''.join((*out, text[copied:]))


def _open_start(text):
    """Line offset of the earliest token whose joined body reaches the last line."""
    for match in _PREFIX.finditer(text):
        index = match.lastindex - 1
        spec, body = SPECS[index], _BODY[index]
        end, count = match.end(), 0
        while end < len(text):
            run = body.match(text, end)
            if run:
                count += run.end() - end
                end = run.end()
            if count >= MAX_WRAPPED_BODY or end >= len(text):
                break
            next_start = end + (2 if text.startswith('\r\n', end) else 1 if text[end] == '\n' else 0)
            if next_start == end:
                break
            if next_start >= len(text):
                return text.rfind('\n', 0, match.start()) + 1
            next_run = body.match(text, next_start)
            if count >= spec.floor:
                line_end = next_run.end() if next_run else next_start
                if (count + line_end - next_start > MAX_WRAPPED_BODY
                        or not (line_end == len(text) or text.startswith('\n', line_end)
                                or text.startswith('\r\n', line_end))):
                    break
            if not next_run:
                break
            end = next_start
    return None


def open_wrapped_line_start(text):
    """Offset of the line from which a stream must hold lines back, or None.

    ``text`` holds only complete lines. A token is open when its joined body
    reaches the last line (same joining rules as _wrapped_spans), so that line may
    still be extended. A finished span that reaches the held line pulls the start
    back to its own line: one line can end a credential and open the next.
    """
    start = _open_start(text)
    if start is None:
        return None
    for first, last in reversed(list(_wrapped_spans(text))):
        if first < start <= last:
            start = text.rfind('\n', 0, first) + 1
    return start


def _wrapped_spans(text):
    """Consume contiguous bodies across LF/CRLF, exactly like redact_wrapped.rs.

    Before the floor a continuation may end mid-line. After it, only whole
    allowed-byte lines join; full contiguous runs are consumed before testing
    floors or the 4096-byte continuation budget (not a per-run truncation).
    """
    copied = 0
    for match in _PREFIX.finditer(text):
        if match.start() < copied:
            continue
        index = match.lastindex - 1
        spec, body = SPECS[index], _BODY[index]
        end, count, wrapped = match.end(), 0, False
        # A value opened by a quote ends at the same quote on a continuation line.
        quote = text[match.start() - 1] if match.start() and text[match.start() - 1] in '"\'' else None
        while True:
            run = body.match(text, end)
            if run:
                count += run.end() - end
                end = run.end()
            if count >= MAX_WRAPPED_BODY:
                break
            if text.startswith('\r\n', end):
                next_start = end + 2
            elif text.startswith('\n', end):
                next_start = end + 1
            else:
                break
            next_run = body.match(text, next_start)
            if count >= spec.floor:
                line_end = next_run.end() if next_run else next_start
                if (count + line_end - next_start > MAX_WRAPPED_BODY
                        or not (line_end == len(text) or text.startswith('\n', line_end)
                                or text.startswith('\r\n', line_end)
                                or (quote and text.startswith(quote, line_end)))):
                    break
            if not next_run:
                break
            end, wrapped = next_start, True
        if wrapped and count >= spec.floor:
            yield match.start(), end
            copied = end


_NAME_KEYWORD = re.compile(r'token|secret|password|passwd|api_?key|access_?key|private_?key|credential')
_LINE_REMAINDER = re.compile(r'[^\r\n]*')


def _wide(char):
    category = unicodedata.category(char)
    return category[0] in 'LNM' or category == 'Pc' or char in '\u200c\u200d'


def _horizontal(char):
    return char.isspace() and char not in '\r\n'


def _name_headers(text):
    """Discover maximal WIDE runs once; failed headers consume maximal H too."""
    index, size = 0, len(text)
    while index < size:
        if not _wide(text[index]):
            index += 1
            continue
        start = index
        while index < size and _wide(text[index]):
            index += 1
        # Per-code-point folding cannot shift offsets into the original text.
        folded = ''.join(chr(ord(c) + 32) if 'A' <= c <= 'Z' else
                         {'ſ': 's', 'K': 'k'}.get(c, c) for c in text[start:index])
        if not _NAME_KEYWORD.search(folded):
            continue
        opening = text[start - 1] if start else ''
        closing = text[index:index + 1]
        subscript = (start >= 12 and opening in ('"', "'") and closing == opening
                     and text[start - 12:start] == 'os.environ[' + opening
                     and text[index + 1:index + 2] == ']')
        header_start = start - 12 if subscript else start - int(opening in ('"', "'"))
        index += 2 if subscript else int(closing in ('"', "'"))
        while index < size and _horizontal(text[index]):
            index += 1
        separator = text[index:index + 1]
        excluded = not subscript and opening == closing == '"' and separator == ':'
        if excluded or separator not in (('=',) if subscript else ('=', ':')):
            continue
        index += 1
        while index < size and _horizontal(text[index]):
            index += 1
        yield header_start, index


_CALL_HEAD = re.compile(r'[A-Za-z_][\w.]*[^\S\r\n]*')
_CLOSER = {'(': ')', '[': ']', '{': '}'}


def is_computed(value, assignment=None):
    """True for None/True/False/null or an already masked value, or for one call, subscript or
    bracketed expression (with attribute/call chains) holding no string literal.

    Spawner-read source keeps these readable; anything else after a credential
    name, such as a literal or a call followed by more text, is masked, as is an
    expression holding another credential-name assignment (the Rust
    redactor mirrors this as is_computed and must be given the whole right-hand side).
    """
    value = value.strip()
    # A credential-name assignment nested in the expression is judged by itself, so it is no computed value.
    if assignment is not None and (assignment.search(value) or next(_name_headers(value), None)):
        return False
    if re.fullmatch(r'None|True|False|null|["\']?\[REDACTED\][\"\']?', value):
        return True
    if '"' in value or "'" in value or not value:
        return False
    head = _CALL_HEAD.match(value)
    at = head.end() if head else 0
    # Only a bare expression may open with a brace; after a name or a group only a call or subscript can follow.
    if value[at:at + 1] not in (_CLOSER if not head else ('(', '[')):
        return False
    while at < len(value):
        if value[at] not in ('(', '[') and not (at == 0 and value[at] in _CLOSER):
            return False
        stack, at = [_CLOSER[value[at]]], at + 1
        while stack and at < len(value):
            char = value[at]
            if char in _CLOSER:
                stack.append(_CLOSER[char])
            elif char == stack[-1]:
                stack.pop()
            elif char in ')]}':
                return False
            at += 1
        if stack:
            return False
        chain = re.match(r'\.[A-Za-z_]\w*', value[at:])
        at += chain.end() if chain else 0
    return True


def redact_wide_assignments(text, assignment, keep=None):
    """OLD wins; NEW consumes physical lines without exposing old wrapped masks.

    ``keep`` is given a value and returns True to leave that assignment readable.
    """
    old_matches = iter(assignment.finditer(text))
    old = next(old_matches, None)
    wrapped = iter(_wrapped_spans(text))
    span = next(wrapped, None)
    out, copied = [], 0
    for start, end in _name_headers(text):
        if start < copied:
            continue
        while old is not None and old.end() <= start:
            old = next(old_matches, None)
        if old is not None and old.start() < end:
            continue  # OLD value containment or intersecting half-open headers.
        value_end = _LINE_REMAINDER.match(text, end).end()
        if keep and keep(text[end:value_end]):
            continue
        if value_end > end:
            # Keep every continuation the pre-A1 wrapped scanner would mask.
            while span is not None and span[0] < value_end:
                value_end = _LINE_REMAINDER.match(text, max(value_end, span[1])).end()
                span = next(wrapped, None)
            out.extend((text[copied:end], '[REDACTED]'))
            copied = value_end
            while old is not None and old.start() < copied:
                old = next(old_matches, None)
    return ''.join((*out, text[copied:]))


def redact_diff_wrapped(text):
    """Mask wrapped tokens in a unified diff, scanning the old and the new side apart.

    Each hunk is read as two files: the old side is its ' ' and '-' lines, the new
    side its ' ' and '+' lines, both without the one-character marker. A line that
    either side masks is masked; a masked run that continues over a line break
    drops the continuation's marker and text, as the plain scan does.
    """
    out, body, in_hunk = [], [], False
    for line in re.findall(r'[^\n]*\n|[^\n]+', text):
        if line.startswith('diff --git '):
            out.extend(_mask_hunk(body))
            body, in_hunk = [], False
        elif line.startswith('@@'):
            out.extend(_mask_hunk(body))
            body, in_hunk = [], True
            out.append(line)
            continue
        (body if in_hunk else out).append(line)
    out.extend(_mask_hunk(body))
    return ''.join(out)


def _mask_hunk(lines):
    """Lines of one hunk with wrapped tokens masked; masks carry the id of the span that made them."""
    marked = [bool(line[:1] in ('+', '-', ' ')) for line in lines]
    masks = [[0] * (len(line) - 1) if mark else [] for line, mark in zip(lines, marked)]
    span_id = 0
    for side in ('-', '+'):
        members = [n for n, (line, mark) in enumerate(zip(lines, marked)) if mark and line[0] in (' ', side)]
        stream = ''.join(lines[n][1:] for n in members)
        offsets, total = [], 0
        for n in members:
            offsets.append(total)
            total += len(lines[n]) - 1
        at = 0
        for first, last in _wrapped_spans(stream):
            span_id += 1
            while offsets[at] + len(masks[members[at]]) <= first:
                at += 1
            cursor = at
            while cursor < len(members) and offsets[cursor] < last:
                row = masks[members[cursor]]
                low, high = max(first - offsets[cursor], 0), min(last - offsets[cursor], len(row))
                row[low:high] = [span_id] * (high - low)
                cursor += 1
    out, join = [], False
    for at, (line, mark, row) in enumerate(zip(lines, marked, masks)):
        if not mark:
            out.append(line)
            join = False
            continue
        content = line[1:].rstrip('\r\n')
        if not join:
            out.append(line[0])
        in_run = join
        for char, flag in zip(content, row):
            if flag:
                if not in_run:
                    out.append('[REDACTED]')
                in_run = True
            else:
                out.append(char)
                in_run = False
        terminator = line[1 + len(content):]
        # The break goes only where the next line continues this very span.
        nxt = masks[at + 1][:1] if at + 1 < len(lines) and marked[at + 1] else []
        join = bool(terminator) and in_run and bool(nxt) and nxt[0] != 0 and nxt[0] == row[-1]
        if not join:
            out.append(terminator)
    return out
