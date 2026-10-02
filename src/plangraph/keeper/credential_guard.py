r"""D-374: keep credential-shaped text out of item definitions and rendered pages.

A ``word/secretish`` token (``\w+/\w{6,}``) within 120 characters of
``credential|password|passwd|pwd|login`` is how a pasted ``user/password`` pair
looks.  ``create``/``edit``/``import`` refuse it (naming the field, never
echoing the value) and ``pg render`` masks any that got in as ``[redacted]``.
A URL path (``://`` in the token) and a clear file path (a ``.ext`` in the
token, or a leading ``data/ docs/ src/ tools/``) are not credentials.
"""
from __future__ import annotations

import re

from .errors import Refused

_SHAPE = re.compile(r"\w+/\w{6,}")
_WORD = re.compile(r"credential|password|passwd|pwd|login", re.IGNORECASE)
_DIRS = ("data/", "docs/", "src/", "tools/")
_NEAR = 120
#: the text fields a create/edit/import carries (``description`` folds into note).
GUARDED_FIELDS = ("title", "note", "done_criteria", "description")


def _token(text, match):
    start, end = match.start(), match.end()
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end]


def _exempt(text, match):
    token = _token(text, match)
    return ("://" in token or re.search(r"\.\w", token) is not None
            or token.lstrip("`'\"([<").startswith(_DIRS))


def find_shapes(text):
    """The credential-shaped matches in *text* (empty when there are none)."""
    if not isinstance(text, str):
        return []
    words = [m.span() for m in _WORD.finditer(text)]
    return [m for m in _SHAPE.finditer(text)
            if any(a <= m.end() + _NEAR and b >= m.start() - _NEAR
                   for a, b in words) and not _exempt(text, m)]


def guard(field, text):
    """Refuse credential-shaped *text* of *field*; the value is never echoed."""
    if find_shapes(text):
        raise Refused("validation",
                      f"{field} looks like it contains a credential "
                      "(a word/secret pair next to a credential keyword); "
                      "remove it")


def mask(text):
    """``(text with each credential-shaped match as [redacted], count)``."""
    hits = find_shapes(text)
    for match in reversed(hits):
        text = text[:match.start()] + "[redacted]" + text[match.end():]
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
