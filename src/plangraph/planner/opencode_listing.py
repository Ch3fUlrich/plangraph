"""Parse `opencode models` output shapes: plain, annotated, columnar (D-3259).

The listing has drifted across releases: one id per line ("plain"), ids with
display suffixes on the same line ("annotated"), and whitespace-aligned tables
("columnar"). Output may carry ANSI colour and non-ASCII display text, so raw
bytes are decoded as UTF-8 with replacement: a listing must never collapse to
an empty set just because a sibling path or column is non-ASCII (C9/D12).

Contract per line (after ANSI stripping):

* every whitespace token that is id-shaped and contains ``/`` is a model id;
* a line with no such token contributes its tokens only when exactly one
  id-shaped token is present (a providerless plain id), so header rows like
  ``MODEL PROVIDER`` and annotation words are never admitted as ids.
"""
from __future__ import annotations

import re

__all__ = ["parse_opencode_models"]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ID = re.compile(r"[\w][\w.:/#-]*")
_TRAILING = ":-./#"


def _decode(data) -> str:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    return str(data)


def _candidates(line: str) -> set[str]:
    out = set()
    for token in line.split():
        if (_ID.fullmatch(token) and not token.isdecimal()
                and not token.endswith(tuple(_TRAILING))):
            out.add(token)
    return out


def parse_opencode_models(data) -> set[str]:
    """Every model id in ``data`` (text or UTF-8 bytes); never raises on decode."""
    models: set[str] = set()
    for line in _ANSI.sub("", _decode(data)).splitlines():
        candidates = _candidates(line)
        ids = {token for token in candidates if "/" in token}
        if ids:
            models |= ids
        elif len(candidates) == 1:
            models |= candidates
    return models
