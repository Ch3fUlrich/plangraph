r"""D-374: keep credential-shaped text out of item definitions and rendered pages.

A ``word/secretish`` token (``\w+/\w{6,}``) within 120 characters of
``credential|password|passwd|pwd|login`` is how a pasted ``user/password`` pair
looks.  ``create``/``edit``/``import`` refuse it -- naming the item key and the
field, echoing only the match's first two characters and its length, never the
value or anything adjacent -- and ``pg render`` masks any that got in as
``[redacted]``.

One exemption (PL-33, the narrow replacement for D-388's "NO exemptions"): a
match inside a *path-like* whitespace-delimited token is dropped.  The token
(stripped of surrounding quotes/brackets and of trailing ``.,;:``) must have at
least TWO ``/`` characters, no ``=`` and no ``:`` -- a leading ``project:`` key
prefix is NOT stripped, so any ``:`` disqualifies -- and its last ``/``-segment
must end with a file extension (a ``.`` followed by 1-5 alphanumerics).  So
``docs/plan/manual-triage-2026-10-02.md`` or ``ops/runbooks/login-flow.md`` near
the word "login" passes, while a bare ``user/password1`` pair, a token with one
slash, a URL, a token carrying ``=`` or ``:`` or a path with no file extension
all still match.  Accepted trade-off (the one case D-388's "NO exemptions"
gives up): ``user/pass/word.txt`` style -- two slashes plus a file extension --
IS exempted, so a pair hiding inside such a path (``admin/swordfish/docs.md``)
is not flagged; authors keep secrets out of path-looking strings themselves.

``mask``/``mask_all`` render exactly the set ``find_shapes`` returns, so the
rendered page masks the same matches the guard refuses.
"""
from __future__ import annotations

import re

from .errors import Refused

_SHAPE = re.compile(r"\w+/\w{6,}")
_WORD = re.compile(r"credential|password|passwd|pwd|login", re.IGNORECASE)
_TOKEN = re.compile(r"\S+")
#: a file extension: '.' + 1-5 alphanumerics at the end of the last '/'-segment.
_PATH_EXT = re.compile(r"\.[0-9A-Za-z]{1,5}\Z")
#: quotes/brackets stripped from both ends of a token before the path test.
_ENCLOSERS = "\"'`()[]{}<>"
_NEAR = 120
#: the text fields a create/edit/import carries (``description`` is its own column).
GUARDED_FIELDS = ("title", "note", "done_criteria", "description")


def _path_like(text, start, end):
    """Is the whitespace-token ``text[start:end]`` a file path (PL-33)?

    Surrounding quotes/brackets go from both ends and trailing ``.,;:`` (in any
    mix, so ``"docs/x.md",`` ends up as ``docs/x.md``) from the right."""
    token = text[start:end].lstrip(_ENCLOSERS).rstrip(_ENCLOSERS + ".,;:")
    return (token.count("/") >= 2 and "=" not in token and ":" not in token
            and bool(_PATH_EXT.search(token.rsplit("/", 1)[-1])))


def find_shapes(text):
    """The credential-shaped matches in *text* (empty when there are none).

    Drops a match whose whitespace-delimited token is path-like: two or more
    ``/`` characters, no ``=``, no ``:``, and a last ``/``-segment ending in a
    file extension (see the module docstring).  Everything else is matched as
    before.  ``mask``/``mask_all`` iterate over this same set, so masking stays
    consistent with refusing.
    """
    if not isinstance(text, str):
        return []
    words = [m.span() for m in _WORD.finditer(text)]
    tokens = [t.span() for t in _TOKEN.finditer(text)]
    hits = []
    for m in _SHAPE.finditer(text):
        if not any(a <= m.end() + _NEAR and b >= m.start() - _NEAR
                   for a, b in words):
            continue
        if any(s <= m.start() and m.end() <= e and _path_like(text, s, e)
               for s, e in tokens):
            continue
        hits.append(m)
    return hits


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
