"""Review seats per writer part and the ``diff_ref`` card check (split from routing.py)."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

from plangraph.routing_errors import SpawnRefused

#: A git revision the inline-diff path accepts: no option-looking or spaced refs.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,199}$")


def _check_diff_ref(ref):
    """``card["diff_ref"]`` = ``{"repo": <absolute path>, "base": <rev>, "head": <rev>}``."""
    if (not isinstance(ref, Mapping) or set(ref) != {"repo", "base", "head"}
            or not all(isinstance(ref[k], str) for k in ref)
            or not os.path.isabs(ref["repo"])
            or not _REF_RE.match(ref["base"]) or not _REF_RE.match(ref["head"])):
        raise SpawnRefused('diff_ref must be {"repo": absolute path, "base": rev, "head": rev}')


def seat_family(family):
    """Keeper family of a leg or writer family (review/family.py: claude=anthropic, gpt=openai, qwen=alibaba)."""
    from plangraph.review.family import _family_key
    return _family_key(family)


def writer_parts(writer_families):
    """Keeper families of the writer parts; an unknown writer refuses (``writer_key``, fail closed)."""
    from plangraph.review.family import FamilyError, writer_key
    try:
        return {writer_key(w) for w in writer_families if str(w or "").strip()}
    except FamilyError as exc:
        raise SpawnRefused(str(exc)) from None


def family_spellings(families, legs):
    """``families`` plus every leg family that is one of them for the keeper (claude/anthropic, gpt/openai)."""
    keys = {seat_family(f) for f in families}
    return set(families) | keys | {leg.family for leg in legs if seat_family(leg.family) in keys}


def pick_seats(ordered, writer_families, *, high):
    """Seats covering every writer part (DECISIONS 3.2, keeper ``coverage.uncovered``).

    A part (writer family) is covered by ``need`` distinct seat families other
    than its own (1 standard, 2 high); a high part also needs one non-Claude
    family other than its own.  A seat may share a family with ANOTHER writer
    (D-3084).  Seats are taken in route order, one per family, only while they
    help cover an open part; at least two families are always picked.  Returns
    ``(seats, coverage, open_parts)``.
    """
    from plangraph.keeper.coverage import uncovered
    writers = sorted({seat_family(w) for w in writer_families if w})
    need = 2 if high else 1
    seats, families = [], []
    for leg in ordered:
        fam = seat_family(leg.family)
        if fam in families or (writers and all(fam == w for w in writers)):
            continue
        open_parts = uncovered(writers, families, need=need, sensitive=high)
        if len(families) >= 2 and not any(fam != w for w in open_parts):
            continue
        seats.append(leg)
        families.append(fam)
        if len(families) >= 2 and not uncovered(writers, families, need=need, sensitive=high):
            break
    coverage = tuple({"leg": leg.id, "covers": [w for w in writers if w != seat_family(leg.family)]}
                     for leg in seats)
    return seats, coverage, uncovered(writers, families, need=need, sensitive=high)
