"""Seat coverage: every writer family must be covered by the reviewer seats.

Mirrors ``tools/seat_verdict.coverage`` on the keeper side.  The reviewer
families of the current round are the *seats*; the families named by the
``Writer:`` trailer of every commit behind the item's current-epoch verified
``commit+ci`` evidence are the *writers*.  A commit may name several
writers (a squashed branch carries one ``Writer:`` line per writer), so every
value of the ``Writer:`` key counts — not just the first or the last — and the
key is matched case-insensitively, as git trailer keys are (``Writer:`` and
``writer:`` are the same key).  A writer family is covered when
``need`` distinct seat families other than it exist (``1`` standard, ``2``
high) and, for a sensitive (high) item, at least one of them is neither
``anthropic`` nor the writer itself.  Two Claude seats are therefore one
family, and Claude can never cover a Claude hand on a high item.

Family names (and the one model-id table) come from
:mod:`plangraph.review.family`; this module copies neither.  A ``Writer:``
value is ``<model-id> <run id>`` and family/alias tokens resolve ONLY in the
model/provider position -- the token(s) before the run id, split on ``/`` and
into ``-``/``.``/``_`` segments -- never inside a run-id slug, where ``meta``,
``google`` and ``nvidia`` are words in a date-stamped name rather than a
second writer.  The check fails closed on any writer it cannot resolve, with
three distinct reasons: a commit with no ``Writer`` or ``Hand-Edit`` trailer,
a blank value or a git error raises ``Refused('writer_unknown', ...)``, a
value whose model position names no known family raises
``Refused('writer_unknown_family', ...)``, and a value naming two writer
families raises ``Refused('writer_mixed_family', ...)``, so a closing verdict
never rests on a guess.  ``Hand-Edit`` also counts as ``anthropic`` alongside
any ``Writer``: a hand edit
is Claude/operator work, not a model writer.
"""
from __future__ import annotations

import re

from ..review import family as review_family
from .errors import Refused

__all__ = ["uncovered", "writer_families"]


def _key_values(trailer, name):
    """Every value of *name*'s trailer key, matched CASE-INSENSITIVELY.

    Git trailer keys are case-insensitive, so ``Writer:`` and ``writer:`` are
    the same key and a commit may carry both spellings.  A plain
    ``trailer.get("Writer")`` would silently ignore the lower-case line and
    let the seat-coverage check close the item without a seat for that writer.
    """
    values = []
    for key, value in trailer.items():
        if isinstance(key, str) and key.strip().lower() == name.lower():
            # Preserve blank entries: a blank Writer must not disappear next
            # to a valid writer or Hand-Edit attribution.
            values.extend(value if isinstance(value, (list, tuple, set)) else [value])
    return values


def uncovered(writers, seats, need=2, sensitive=False):
    """Sorted writer families not covered by *seats*.

    Identical in result to ``tools.seat_verdict.coverage``: blank entries are
    ignored, all ``anthropic`` seats count once, a writer is covered by
    ``need`` distinct other families and, when *sensitive*, by at least one
    non-anthropic family that is not the writer itself.
    """
    writer_set = sorted({str(w).strip().lower() for w in writers
                         if str(w).strip()})
    seat_families = {str(seat).strip().lower() for seat in seats
                     if str(seat).strip()}
    missing = []
    for writer in writer_set:
        others = {family for family in seat_families if family != writer}
        covered = len(others) >= need
        if covered and sensitive:
            covered = any(family != "anthropic" and family != writer
                          for family in seat_families)
        if not covered:
            missing.append(writer)
    return sorted(missing)


def writer_families(store, item, ctx):
    """Writer families of *item*'s current-epoch verified ``commit+ci`` rows.

    Every ``Writer:`` value of every leaf commit behind those rows is
    resolved, so a squashed commit naming two writer families contributes
    both.  The refusals are fail closed and distinct: ``writer_unknown`` for
    a missing or blank attribution and for any git error,
    ``writer_unknown_family`` for a value whose model position names no
    known family, ``writer_mixed_family`` for a value that names two.  An
    item with no commit
    evidence (e.g. decision-only) has no commit writers and returns ``[]``.
    """
    rows = [e for e in store._evidence(item["key"])
            if e["epoch"] == item["epoch"] and e["verified"]
            and e["kind"] == "commit+ci"
            and (e.get("ref") or {}).get("commit")]
    if not rows:
        return []
    git = getattr(ctx, "git", None)
    if git is None:
        raise Refused("writer_unknown", "no git adapter for commit evidence")
    repo = git.repo_path(item["project"])
    if not repo:
        raise Refused("writer_unknown",
                      f"no repo registered for project {item['project']!r}")
    families = []
    for row in rows:
        commit = row["ref"]["commit"]
        try:
            trailers = git.writer_trailers(repo, commit)
        except Exception as exc:  # noqa: BLE001 - fail closed on any git error
            raise Refused("writer_unknown",
                          f"cannot read writer trailers for {commit}: {exc}"
                          ) from exc
        if not trailers:
            raise Refused("writer_unknown",
                          f"commit {commit} names no writer")
        for trailer in trailers:
            writers = _key_values(trailer, "Writer")
            if writers:
                for writer in writers:
                    if writer is None or not str(writer).strip():
                        raise Refused("writer_unknown", "blank Writer value")
                    # Only the model/provider position may name a family; a
                    # run-id slug is a date-stamped name, not a writer.
                    tokens = _model_tokens(writer)
                    if not tokens:
                        # Punctuation or a bare run id: no model/provider
                        # token at all, so the attribution is blank.
                        raise Refused(
                            "writer_unknown",
                            f"Writer value names no model token: {writer!r}")
                    resolved = {family for token in tokens
                                for family in _writer_token_families(token)}
                    if len(resolved) > 1:
                        raise Refused(
                            "writer_mixed_family",
                            f"Writer value names several writer families: {writer!r}")
                    if not resolved:
                        raise Refused(
                            "writer_unknown_family",
                            f"Writer value names no known writer family: {writer!r}")
                    families.append(resolved.pop())
            if any(value is not None and str(value).strip()
                   for value in _key_values(trailer, "Hand-Edit")):
                families.append("anthropic")
            elif not writers:
                raise Refused(
                    "writer_unknown",
                    f"commit behind {commit} has neither Writer nor Hand-Edit")
    return families


# Single-family launchers still name a writer family; ignoring them can hide
# a second writer. Multi-family launchers are not model identities.
_WRITER_ALIASES = dict.fromkeys(
    ("qodercli", "qoder", "qoder-cli", "qoder-agent"), "alibaba")
_WRITER_ALIASES["l2"] = "anthropic"
_WRITER_ALIASES.update({"cx": "openai", "codex": "openai",
                        "mimo": "xiaomi", "xiaomi-mimo": "xiaomi"})
_WRITER_ALIASES.update({family: family for _, family in review_family.FAMILIES})
_WRITER_ALIASES.update(dict.fromkeys(
    "agy opencode omniroute openrouter spawner tool-less tool-using".split()))


def _writer_token_families(token):
    """Keep every match; punctuation cannot conceal a second model or alias."""
    low = token.lower()
    if low in _WRITER_ALIASES:
        family = _WRITER_ALIASES[low]
        return {family} if family else set()
    found = set()
    for part in [low, *re.split(r"[._-]+", low)]:
        if part in _WRITER_ALIASES:
            family = _WRITER_ALIASES[part]
            if family:
                found.add(family)
        else:
            found.update(family for pattern, family in review_family.FAMILIES
                         if re.search(pattern, part))
    return found


#: The first segment of a run id: date/time stamped, as in
#: ``20261024-061038-meta-review-18b0fc`` or ``20261024-1-nvidia-driver-x``.
_RUN_ID_RE = re.compile(r"^\d{4,8}[-_]")


def _model_position(value):
    """*value* with its run id removed: where model/provider tokens live.

    A ``Writer:`` value is ``<model-id> <run id>`` (the run id is optional).
    From the first date-stamped segment on, the value is a slug of names --
    ``...-meta-review-...``, ``...-google-sync-...``, ``...-nvidia-driver-x``
    -- and no family or alias may resolve there: those words are a run, not a
    second writer.  A value that is only a run id therefore has no model
    position at all (and so no family: it fails closed).  Tokens before the
    stamp are untouched, so a second MODEL token such as ``codex`` in
    ``deepseek-v4 codex`` still names its own family.
    """
    parts = str(value).split()
    for index, part in enumerate(parts):
        if _RUN_ID_RE.match(part):
            return " ".join(parts[:index])
    return str(value)


def _model_tokens(value):
    """The model/provider tokens of *value*: the run id contributes none.

    The position is split on ``/`` (``omniroute/cx-gpt-6.1-sol``) and each
    segment on ``-``/``.``/``_`` (``cx``), exactly as it always was -- only
    the run id is excluded.  Empty when the value holds no model token at
    all, which tells a blank attribution (``:;,/()[]...---``, a bare run id)
    apart from a model id the table cannot resolve.
    """
    return [token for token in
            (part.strip(".-") for part in
             re.split(r"[^A-Za-z0-9._-]+", _model_position(value)))
            if token and any(ch.isalnum() for ch in token)]
