"""Rust worker-core's shared prefixed-token and wrapped-body policy."""
import re
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


# Check for a keyword once, then consume the whole name without retrying every
# keyword occurrence when the separator is absent (TOKEN repeated took >80 s).
_NEW_NAME = (r'(?=[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|'
             r'ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL))[A-Za-z0-9_]++')
_OLD_VALUE = r'''"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\r\n]+'''
# Rust rs[3]/rs[4]: complete quotes and placeholders consume a glued tail;
# unquoted values stop at closing delimiters or &, not commas or spaces.
# Possessive repetitions keep failed complete quotes/continuations linear.
_NEW_VALUE = (
    r'''(?P<quoted>"(?:[^"\\\n]|\\(?:\r?\n|.))*+"|'''
    r"'(?:[^'\\\n]|\\(?:\r?\n|.))*+')" + r'''[^\s,;"'}\])&]*+|'''
    r'''\[REDACTED\](?:\[REDACTED\]|[^\s,;"'}\])&])*+|'''
    r'''(?:\\\r?\n|[^\s"'}\])&])(?:\\\r?\n|[^\n\r}\])&])*+|'''
    r'''"(?:\\\r?\n|[^\n\r])*+|'(?:\\\r?\n|[^\n\r])*+|'''
    r'(?(equals)[}\])][^\n\r}\])&]++|(?!))')
_ASSIGNMENT = re.compile(
    r'''(?im)((?:["'])?\b(?P<old>\w*_KEY|PASSWORD|PG_DSN|TOKEN|SECRET)'''
    r'''(?:["'])?\s*[=:]\s*|(?P<json>"\b''' + _NEW_NAME +
    r'''"[^\S\r\n]*:[^\S\r\n]*)|(?:["'])?\b''' + _NEW_NAME +
    r'''(?:["'])?[^\S\r\n]*(?:(?P<equals>=)|:)[^\S\r\n]*)'''
    r'(?(old)(?:' + _OLD_VALUE + r')|(?:(?(json)(?P<scalar>'
    r'-?\d[\d.eE+-]*|(?-i:true|false|null))(?=[,}\]\s]|$)|(?!))|'
    + _NEW_VALUE + r'))')


def _redact_assignments(text):
    def replace(m):
        if m['scalar'] is not None:
            # D-5279 R3: NEW double-quoted JSON keys only. Always mask the
            # scalar, including numeric credentials, but preserve later fields.
            return m[1] + '"[REDACTED]"'
        value = m[0][len(m[1]):] if m['old'] else m['quoted']
        return m[1] + (value[0] + '[REDACTED]' + value[-1]
                       if value and value[0] in '"\x27' and value[-1] == value[0]
                       else '[REDACTED]')
    return _ASSIGNMENT.sub(replace, text)


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
