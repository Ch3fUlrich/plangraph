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


def open_wrapped_line_start(text):
    """Offset of the line holding the earliest token that later lines may still extend.

    ``text`` holds only complete lines. A token is open when its joined body
    reaches the last line, so a stream must keep that line until more arrives
    (same joining rules as _wrapped_spans).
    """
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


def redact_wide_assignments(text, assignment):
    """OLD wins; NEW consumes physical lines without exposing old wrapped masks."""
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
    """Mask wrapped tokens in a unified diff, joining body lines across their markers.

    The one-character +/-/space marker of each hunk line is not part of the
    credential, so the scan runs on the marker-free lines; a line inside a masked
    span disappears with its marker, the others keep theirs.
    """
    lines, marks, in_hunk = re.findall(r'[^\n]*\n|[^\n]+', text), [], False
    for line in lines:
        if line.startswith('diff --git '):
            in_hunk = False
        elif line.startswith('@@'):
            in_hunk = True
        marks.append(1 if in_hunk and line[:1] in ('+', '-', ' ') else 0)
    stripped = ''.join(line[mark:] for line, mark in zip(lines, marks))
    spans = list(_wrapped_spans(stripped))
    if not spans:
        return text
    out, offset, spans_at = [], 0, 0
    for line, mark in zip(lines, marks):
        start, end = offset, offset + len(line) - mark
        offset = end
        while spans_at < len(spans) and spans[spans_at][1] <= start:
            spans_at += 1
        span = spans[spans_at] if spans_at < len(spans) else None
        if span is None or span[0] >= end:
            out.append(line)
        elif span[0] >= start:
            out.extend((line[:mark + span[0] - start], '[REDACTED]'))
            if span[1] < end:
                out.append(line[mark + span[1] - start:])
        elif span[1] < end:
            out.append(line[mark + span[1] - start:])
    return ''.join(out)
