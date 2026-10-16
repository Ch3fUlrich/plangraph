r"""D-374: keep credential-shaped text out of item definitions and rendered pages.

A ``word/secretish`` token (``\w+/\w{6,}``) within 120 characters of
``credential|password|passwd|pwd|login`` is how a pasted ``user/password`` pair
looks.  ``create``/``edit``/``import`` refuse it -- naming the item key and the
field, echoing only the match's first two characters and its length, never the
value or anything adjacent -- and ``pg render`` masks any that got in as
``[redacted]``.

There are NO exemptions (D-388): a URL, a path or a ``.ext`` does not hide a
pair, so authors rephrase paths and URLs that sit near a credential word.

``mask``/``mask_all`` render exactly the set ``find_shapes`` returns, so the
rendered page masks the same matches the guard refuses.
"""
from __future__ import annotations

import re

from .errors import Refused

_SHAPE = re.compile(r"\w+/\w{6,}")
_WORD = re.compile(r"credential|password|passwd|pwd|login", re.IGNORECASE)
_NEAR = 120
#: the text fields a create/edit/import carries (``description`` is its own column).
GUARDED_FIELDS = ("title", "note", "done_criteria", "description", "estimate_src")


def find_shapes(text):
    """The credential-shaped matches in *text* (empty when there are none).

    ``mask``/``mask_all`` iterate over this same set, so masking stays
    consistent with refusing.
    """
    if not isinstance(text, str):
        return []
    words = [m.span() for m in _WORD.finditer(text)]
    return [m for m in _SHAPE.finditer(text)
            if any(a <= m.end() + _NEAR and b >= m.start() - _NEAR
                   for a, b in words)]


def guard(field, text, key=None):
    """Refuse credential-shaped *text* of *field*; the value is never echoed.

    *key*, when the caller knows it, is the normal ``project:ID`` item key and
    is prefixed so a batch refusal says which item failed (PL-33).  Only the
    matched span's first two characters and its length are shown (``ab***
    (len 14)``); the full span and its surroundings are never echoed.
    """
    hits = find_shapes(text)
    if not hits:
        return
    first = hits[0]
    masked = (f"{text[first.start():first.start() + 2]}***"
              f" (len {first.end() - first.start()})")
    raise Refused("validation",
                  f"{key + ': ' if key else ''}{field} looks like it contains "
                  f"a credential at chars {first.start()}-{first.end()} "
                  f"({masked}) ; remove it")


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
