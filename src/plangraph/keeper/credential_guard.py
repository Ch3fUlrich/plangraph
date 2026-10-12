r"""D-374: keep credential-shaped text out of item definitions and rendered pages.

A path candidate - every whitespace-delimited token that carries a ``/`` -
within 120 characters of ``credential|password|passwd|pwd|login`` is how a
pasted ``user/password`` pair looks.  ``create``/``edit``/``import`` refuse a
candidate that is not an exempt repo path (naming the item key, the field and
the character span, never echoing the value) and ``pg render`` masks any that
got in as ``[redacted]``.

D-1090 (supersedes the D-388 "no path exemptions" rule): repo-path-like
strings are NOT credentials.  A match whose whole whitespace-delimited token
is path-like (known file extension with clean segments, or a multi-slash
lowercase path) with no ``=``/``:`` pair and no secret-looking segment is
exempt from both ``guard()`` and ``mask()``.  The refused span is the stripped
token itself, so masking removes the whole path-shaped candidate.

Accepted limits: inside an exempt path ANY chunk that holds a digit is
secret-like except a year (``1999``/``2026``), a 1-2 digit number (the MM or
DD of a ``YYYY-MM-DD`` date, which splits into separate chunks) and a version
token (``v2``/``v10``) - so ``k3s`` is refused as an accepted false positive.
A letters-only chunk is never secret-like (apart from a 12+ letter mixed-case
CamelCase blob), so a lowercase letters-only passphrase path such as
``a/b/correcthorsebattery`` is not detectable.  Because a candidate is any
slash-bearing token, a plain short pair such as ``and/or`` next to a keyword
is refused too - an accepted false positive of the widened net.  A ``key:
value`` pair value of lowercase letters only (no digit, no interior capital)
is read as prose (``token: refresh-after-expiry``) and not refused; a
secret-like pair value is refused whatever follows it, a subscript
``token=abcd1234[0]`` included.
"""
from __future__ import annotations

import re

from .errors import Refused

_WORD = re.compile(r"credential|password|passwd|pwd|login", re.IGNORECASE)
_NEAR = 120
#: the text fields a create/edit/import carries (``description`` is its own column).
GUARDED_FIELDS = ("title", "note", "done_criteria", "description")

_EXT_KEYWORDS = re.compile(
    r"credential|password|passwd|pwd|login|token|secret|api[_-]?key",
    re.IGNORECASE)
_CRED_PAIR = re.compile(
    r"(?<![A-Za-z0-9_])(?:credential|password|passwd|pwd|login|token|secret|api[_-]?key)"
    r"[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9._~+/=-]{8,})(?![A-Za-z0-9_])",
    re.IGNORECASE)
_HEX = re.compile(r"[0-9a-fA-F]{32,}")
_B64 = re.compile(r"[A-Za-z0-9+/=]{32,}={0,2}")
_PEM = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----")

_EXTENSIONS = frozenset([
    "md", "yml", "yaml", "py", "json", "toml", "txt", "sh", "cfg", "ini",
    "conf", "service", "timer", "js", "ts", "html", "css", "sql", "log",
    "csv", "lock", "rst"])
_SEG1 = re.compile(r"[A-Za-z0-9._-]+")
_SEG2 = re.compile(r"[a-z0-9._-]+")
#: the only digit-bearing chunks a repo path is allowed to carry
_CLEAN_CHUNKS = (re.compile(r"(?:19|20)[0-9]{2}"), re.compile(r"[0-9]{1,2}"),
                re.compile(r"v[0-9]{1,3}"))
_WS = " \t\n\r\f\v"
_STRIP_LEAD = "./([<{`'\""
_STRIP_TRAIL = ".,;)]}>`'\""


class _Hit:
    """A minimal ``re.Match``-like span (``start()``/``end()``/``span()``)."""

    __slots__ = ("_s", "_e", "_text")

    def __init__(self, s, e, text=""):
        self._s = int(s)
        self._e = int(e)
        self._text = text

    def group(self, group=0):
        return self._text[self._s:self._e]

    def start(self, group=None):
        return self._s

    def end(self, group=None):
        return self._e

    def span(self, group=None):
        return (self._s, self._e)

    def __repr__(self):
        return f"_Hit({self._s}, {self._e})"


def _strip_token(token):
    return token.lstrip(_STRIP_LEAD).rstrip(_STRIP_TRAIL)


def _path_candidates(text):
    """``(start, end, stripped)`` per whitespace token carrying a ``/``."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] in _WS:
            i += 1
            continue
        j = i
        while j < n and text[j] not in _WS:
            j += 1
        raw = text[i:j]
        if "/" in raw:
            head = raw.lstrip(_STRIP_LEAD)
            start = i + (len(raw) - len(head))
            body = head.rstrip(_STRIP_TRAIL)
            if body:
                out.append((start, start + len(body), body))
        i = j
    return out


def _has_upper(s):
    return any(c.isupper() for c in s)


def _has_lower(s):
    return any(c.islower() for c in s)


def _has_digit(s):
    return any(c.isdigit() for c in s)


def _has_interior_upper(s):
    for i in range(1, len(s)):
        if s[i].isupper() and (s[i - 1].islower() or s[i - 1].isdigit()):
            return True
    return False


def _secret_segment(seg):
    """ONE rule: a digit-bearing chunk is secret-like, except year/MM/DD/vN.

    Letters-only chunks are clean apart from a 12+ letter mixed-case
    CamelCase blob.
    """
    stem = seg.rsplit(".", 1)[0] if "." in seg else seg
    for chunk in re.split(r"[-_.]", stem):
        if not chunk:
            continue
        if _has_digit(chunk):
            if not any(p.fullmatch(chunk) for p in _CLEAN_CHUNKS):
                return True
        elif len(chunk) >= 12 and chunk.isalpha() and _has_upper(chunk) \
                and _has_lower(chunk):
            return True
    return False


def _is_exempt_token(raw):
    token = _strip_token(raw)
    if not token or "/" not in token:
        return False
    if "=" in token:
        return False
    if "://" in token:
        return False
    if ":" in token:
        return False
    segments = token.split("/")
    if any(not s for s in segments):
        return False
    # Branch 1: known file extension, clean segments, no secret segment.
    if "." in segments[-1]:
        ext = segments[-1].rsplit(".", 1)[-1].lower()
        if ext in _EXTENSIONS and all(_SEG1.fullmatch(s) for s in segments):
            if not any(_secret_segment(s) for s in segments):
                return True
    # Branch 2: >= 2 slashes, all lowercase, no secret-looking chunk.
    if token.count("/") >= 2 and all(_SEG2.fullmatch(s) for s in segments):
        return not any(_secret_segment(s) for s in segments)
    return False


def _token_of(text, pos):
    start = pos
    while start > 0 and text[start - 1] not in " \t\n\r\f\v":
        start -= 1
    end = pos
    while end < len(text) and text[end] not in " \t\n\r\f\v":
        end += 1
    return text[start:end]


def _shape_spans(text):
    words = [m.span() for m in _WORD.finditer(text)]
    out = []
    for start, end, token in _path_candidates(text):
        if not any(a <= end + _NEAR and b >= start - _NEAR
                   for a, b in words):
            continue
        if _is_exempt_token(token):
            continue
        out.append((start, end))
    return out


def _pair_spans(text):
    out = []
    for m in _CRED_PAIR.finditer(text):
        start, end = m.start(1), m.end(1)
        val = text[start:end]
        if "[" in val or "]" in val:
            continue
        # Operator rule: a key=value/credential pair is never path-exempt,
        # so there is no _is_exempt_token skip here, and no lookahead skip
        # either: a secret-like value is refused whatever follows it, a
        # ``[0]`` subscript or a trailing "(rotate)" included.  A
        # slash-bearing pair value is always refused; any other pair value
        # must be secret-like (a digit or an interior uppercase letter) to
        # count, so a letters-only value such as ``token=tokens[0]`` is
        # prose.
        if "/" not in val:
            if not (_has_digit(val) or _has_interior_upper(val)):
                continue
        out.append((start, end))
    return out


def _run_spans(text):
    out = []
    keys = [m.start() for m in _EXT_KEYWORDS.finditer(text)]
    if not keys:
        return out
    for m in _HEX.finditer(text):
        if any(k <= m.start() <= k + 60 for k in keys):
            if _is_exempt_token(_token_of(text, m.start())):
                continue
            out.append((m.start(), m.end()))
    for m in _B64.finditer(text):
        raw = m.group(0)
        if len(raw) < 32:
            continue
        core = raw.strip("=")
        if len(set(core)) < 4:
            continue
        if not ((_has_digit(core) and any(c.isalpha() for c in core))
                or (_has_upper(core) and _has_lower(core))):
            continue
        if any(k <= m.start() <= k + 60 for k in keys):
            if _is_exempt_token(_token_of(text, m.start())):
                continue
            out.append((m.start(), m.end()))
    return out


def _pem_spans(text):
    return [(m.start(), m.end()) for m in _PEM.finditer(text)]


def _all_spans(text):
    if not isinstance(text, str) or not text:
        return []
    spans = _shape_spans(text) + _pair_spans(text) + _run_spans(text) \
        + _pem_spans(text)
    spans.sort()
    merged = []
    for s, e in spans:
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def find_shapes(text):
    """The credential-shaped matches in *text* (empty when there are none)."""
    return [_Hit(s, e, text) for s, e in _all_spans(text)]


def guard(field, text, key=None):
    """Refuse credential-shaped *text* of *field*; the value is never echoed."""
    hits = _all_spans(text) if isinstance(text, str) else []
    if hits:
        s, e = hits[0]
        n = e - s
        masked = "*" * min(n, 8)
        if key:
            raise Refused("validation",
                          f"item {key}: {field} looks like it contains a "
                          f"credential at chars {s}-{e} ({n} chars, masked "
                          f"{masked}); remove or reword it")
        raise Refused("validation",
                      f"{field} looks like it contains a credential at chars "
                      f"{s}-{e} ({n} chars, masked {masked}); remove or "
                      "reword it")


def mask(text):
    """``(text with each credential-shaped match as [redacted], count)``."""
    hits = _all_spans(text) if isinstance(text, str) else []
    if not isinstance(text, str):
        return text, 0
    for s, e in reversed(hits):
        text = text[:s] + "[redacted]" + text[e:]
    return text, len(hits)


def mask_all(value):
    """Mask every string inside a JSON-like *value*; ``(value, count)``."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        pairs = [mask_all(v) for v in value]
        return [p[0] for p in pairs], sum(p[1] for p in pairs)
    if isinstance(value, dict):
        pairs = {k: mask_all(v) for k, v in value.items()}
        return ({k: p[0] for k, p in pairs.items()},
                sum(p[1] for p in pairs.values()))
    return value, 0
