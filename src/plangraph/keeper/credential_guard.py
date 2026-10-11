r"""D-374: keep credential-shaped text out of item definitions and rendered pages.

A ``word/secretish`` token (``\w+/\w{6,}``) within 120 characters of
``credential|password|passwd|pwd|login`` is how a pasted ``user/password`` pair
looks.  ``create``/``edit``/``import`` refuse it (naming the item key, the
field and the character span, never echoing the value) and ``pg render`` masks
any that got in as ``[redacted]``.

D-1090 (supersedes the D-388 "no path exemptions" rule): repo-path-like
strings are NOT credentials.  A match whose whole whitespace-delimited token
is path-like (known file extension with clean segments, or a multi-slash
lowercase path) with no ``=``/``:`` pair and no secret-looking segment is
exempt from both ``guard()`` and ``mask()``.

Accepted limits: lowercase letters-only passphrase paths with >=2 slashes,
CamelCase alphabetic file names, and single-slash lowercase pairs without
a known extension are judged as before / not detectable as credentials;
``src/my_module`` stays refused as a single-slash lowercase pair.  A
``key: value`` pair value of lowercase letters only (no digit, no interior
capital) is read as prose (``token: refresh-after-expiry``) and not refused.
"""
from __future__ import annotations

import re

from .errors import Refused

_SHAPE = re.compile(r"\w+/\w{6,}")
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
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


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
    return token.lstrip("./([<{`'\"").rstrip(".,;)]}>`'\"")


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
    stem = seg.rsplit(".", 1)[0] if "." in seg else seg
    chunks = re.split(r"[-_.]", stem)
    date_prefix = (
        len(chunks) >= 3 and re.fullmatch(r"[0-9]{4}", chunks[0])
        and re.fullmatch(r"[0-9]{2}", chunks[1])
        and re.fullmatch(r"[0-9]{2}", chunks[2]))
    mixed = 0
    for idx, chunk in enumerate(chunks):
        if not chunk:
            continue
        if date_prefix and idx < 3:
            continue
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", chunk):
            continue
        letters = any(c.isalpha() for c in chunk)
        digits = any(c.isdigit() for c in chunk)
        if len(chunk) >= 5 and digits and not letters:
            return True   # a bare number run (``AbCdEf-123456``), dates aside
        if len(chunk) >= 6 and letters and digits:
            return True
        if len(chunk) >= 8 and _has_upper(chunk) and _has_lower(chunk) \
                and _has_digit(chunk):
            return True
        if len(chunk) >= 12 and re.fullmatch(r"[A-Za-z0-9+/=]+", chunk):
            upper = _has_upper(chunk)
            lower = _has_lower(chunk)
            if (letters and digits) or (upper and lower):
                return True
        # Several short letter+digit chunks (``abc1_def2``) are one secret
        # split by separators; a bare version token (``v2``) does not count.
        if letters and digits and not re.fullmatch(r"v?[0-9]{1,3}", chunk):
            mixed += 1
    return mixed >= 2


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
        for s in segments:
            if _DATE.fullmatch(s):
                continue
            if _secret_segment(s):
                return False
        return True
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
    for m in _SHAPE.finditer(text):
        if not any(a <= m.end() + _NEAR and b >= m.start() - _NEAR
                   for a, b in words):
            continue
        if _is_exempt_token(_token_of(text, m.start())):
            continue
        out.append((m.start(), m.end()))
    return out


def _pair_spans(text):
    out = []
    for m in _CRED_PAIR.finditer(text):
        start, end = m.start(1), m.end(1)
        val = text[start:end]
        if "[" in val or "]" in val:
            continue
        # Operator rule: a key=value/credential pair is never path-exempt,
        # so there is no _is_exempt_token skip here.  A trailing "[" still
        # exempts index access such as a tokens dict subscript; every other
        # post-value lookahead skip is gone, so a trailing "(rotate)"
        # no longer exempts the pair.  A slash-bearing pair
        # value is always refused; any other pair value must be secret-like
        # (a digit or an interior uppercase letter) to count.
        rest = text[end:].lstrip()[:1]
        if rest == "[":
            continue
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
