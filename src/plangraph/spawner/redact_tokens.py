"""Rust worker-core's shared prefixed-token and wrapped-body policy."""
import json
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
    if '\ue000' in text or '\ue001' in text:
        return text
    names = []

    def shield(m):
        names.append(m[1])
        return '\ue000' + str(len(names) - 1) + '\ue001' + m[0][len(m[1]):]

    text = redact_wrapped(assignment.sub(shield, text))
    return re.sub('\ue000([0-9]+)\ue001', lambda m: names[int(m[1])], text)


def redact_wrapped(text):
    """Consume contiguous bodies across LF/CRLF, exactly like redact_wrapped.rs.

    Before the floor a continuation may end mid-line. After it, only whole
    allowed-byte lines join; full contiguous runs are consumed before testing
    floors or the 4096-byte continuation budget (not a per-run truncation).
    """
    out, copied = [], 0
    for match in _PREFIX.finditer(text):
        if match.start() < copied:
            continue
        index = match.lastindex - 1
        spec, body = SPECS[index], _BODY[index]
        end, count, wrapped = match.end(), 0, False
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
                                or text.startswith('\r\n', line_end))):
                    break
            if not next_run:
                break
            end, wrapped = next_start, True
        if wrapped and count >= spec.floor:
            out.extend((text[copied:match.start()], '[REDACTED]'))
            copied = end
    return ''.join((*out, text[copied:]))


def _redact_pem(text):
    # Scan markers once: a lazy complete-block regex retries every unterminated
    # BEGIN against the whole suffix, quadratic on adversarial prompt text.
    out, copied, start = [], 0, None
    for match in re.finditer(r'-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----', text):
        if match[1] == 'BEGIN' and start is None:
            start = match.start()
        elif match[1] == 'END' and start is not None:
            out.extend((text[copied:start], '[REDACTED]'))
            copied, start = match.end(), None
    if start is not None:
        return ''.join((*out, text[copied:start], '[REDACTED]'))
    return ''.join((*out, text[copied:]))


def _is_numeric(value):
    """True for optionally signed decimal digits only: "-1", "+8080", "8080".

    Such values are configuration (sentinel, port, limit), never credential
    material, so a configured provider env name may expose them. Every other
    literal form stays redacted: "1_000" (digit grouping), "0x1F", "1.5" and
    friends can carry a secret payload.
    """
    return re.fullmatch(r'[+-]?\d+', value) is not None




_NAME_KEYWORD = re.compile(r'token|secret|password|passwd|api_?key|access_?key|private_?key|credential')
_LINE_REMAINDER = re.compile(r'[^\r\n]*')
_JSON_SCALAR = re.compile(r'"(?:[^"\\\x00-\x1F]|\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4}))*"'
                          r'|true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
_JSON_BLANKS = re.compile(r'[ \t]*')
_INTEGER_OBJECT = object()
_COUNTERS = json.JSONDecoder(object_pairs_hook=lambda pairs: _INTEGER_OBJECT if
    len({key for key, _ in pairs}) == len(pairs) and
    all(type(value) is int for _, value in pairs) else None)


def _counter_end(text, start, end):
    """Only exact tokens objects with direct integer members survive; doubt fails closed."""
    if not text.startswith('{', end) or not re.match(r'"tokens"\s*:', text[start:end]):
        return None
    try:
        value, stop = _COUNTERS.raw_decode(text, end)
    except (ValueError, RecursionError):
        return None
    boundary = _JSON_BLANKS.match(text, stop).end()
    if (value is _INTEGER_OBJECT and text.find('\r', end, stop) < 0
            and text.find('\n', end, stop) < 0
            and (boundary == len(text) or text[boundary] in ',}]\r\n')):
        return stop
    return None


def _wide(char):
    category = unicodedata.category(char)
    return category[0] in 'LNM' or category == 'Pc' or char in '\u200c\u200d'


def _horizontal(char):
    return char.isspace() and char not in '\r\n'


def _name_headers(text, json_scalar=False):
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
        if excluded != json_scalar or separator not in (('=',) if subscript else ('=', ':')):
            continue
        index += 1
        while index < size and _horizontal(text[index]):
            index += 1
        resume = yield header_start, index
        index = max(index, resume or 0)


def redact_wide_assignments(text, assignment, *, json_scalar=False):
    """OLD wins; plain values consume to CR/LF, exact JSON keys mask one scalar."""
    old_matches = iter(assignment.finditer(text))
    old = next(old_matches, None)
    out, copied = [], 0
    headers = _name_headers(text, json_scalar)
    while True:
        try:
            start, end = headers.send(copied or None)
        except StopIteration:
            break
        if start < copied:
            continue
        while old is not None and old.end() <= start:
            old = next(old_matches, None)
        if old is not None and old.start() < end:
            continue  # OLD value containment or intersecting half-open headers.
        counter_end = _counter_end(text, start, end) if json_scalar else None
        if counter_end is not None:
            out.append(text[copied:counter_end])
            copied = counter_end
            continue
        scalar = _JSON_SCALAR.match(text, end) if json_scalar else None
        boundary = _JSON_BLANKS.match(text, scalar.end()).end() if scalar else end
        clean = scalar and (boundary == len(text) or text[boundary] in ',}]\r\n')
        value_end = scalar.end() if clean else _LINE_REMAINDER.match(text, end).end()
        if value_end > end or json_scalar:
            out.extend((text[copied:end], '"[REDACTED]"' if json_scalar else '[REDACTED]'))
            copied = value_end
            while old is not None and old.start() < copied:
                old = next(old_matches, None)
    return ''.join((*out, text[copied:]))
