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
value is ``<model-id> <run id>``, read by one strict four-rule grammar
(G1-G4) that decides where model words live and when a value fails closed:

G1 -- the model/provider position is the whitespace-delimited token(s)
before the run id; G2 -- the first date-stamped token (4-8 digits followed
by ``-`` or ``_``) ENDS that position, so trailing text after the run id is
slug text (``meta``, ``google`` and ``nvidia`` are words in a
date-stamped name), never a second writer; G3 -- a masked id segment
(``<hex>``, ``<uuid>``) is run-id shaped and contributes no model token;
G4 -- each remaining token splits into words, every word resolving through
the alias table first and the family table second, and the FIRST word of
the first word-bearing token -- and of any token that names a family --
must be a word those tables know, else the value fails closed as
``writer_unknown_family`` even when a later token resolves: ``kimi-k2
codex`` is not openai and ``run-claude`` never hides behind its
``claude``.

The check fails closed on any writer it cannot resolve, with three
distinct reasons: a commit with no ``Writer`` or ``Hand-Edit`` trailer, a
blank value, a value with no model token at all, or a git error raises
``Refused('writer_unknown', ...)``, a value whose model position names no
known family raises ``Refused('writer_unknown_family', ...)``, and a
value naming two writer families raises ``Refused('writer_mixed_family',
...)``, so a closing verdict never rests on a guess.  ``Hand-Edit`` also
counts as ``anthropic`` alongside any ``Writer``: a hand edit is
Claude/operator work, not a model writer.
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
    resolved under the G1-G4 grammar of the module docstring, so a squashed
    commit naming two writer families contributes both.  The refusals are
    fail closed and distinct: ``writer_unknown`` for a missing or blank
    attribution, a value with no model token, and any git error,
    ``writer_unknown_family`` for a value whose model position names no
    known family or whose first word the tables do not know (G4),
    ``writer_mixed_family`` for a value that names two.  An item with no
    commit evidence (e.g. decision-only) has no commit writers and
    returns ``[]``.
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
                    # Only the model/provider position may name a family,
                    # and only under the G1-G4 grammar: a run-id slug is a
                    # date-stamped name, a masked segment is no token at
                    # all, and an unknown model head fails the value closed
                    # -- never guessed from a later token.
                    families.append(_writer_value_family(writer))
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


def _writer_word_families(word):
    """The writer families of ONE word: the alias table wins, else FAMILIES.

    The alias table is consulted first and alone: a word it knows resolves
    by that entry only -- even an alias carrying no family of its own
    (``opencode``, ``spawner``), which then names none and can never borrow
    one from a pattern search.
    """
    if word in _WRITER_ALIASES:
        family = _WRITER_ALIASES[word]
        return {family} if family else set()
    return {family for pattern, family in review_family.FAMILIES
            if re.search(pattern, word)}


def _known_word(word):
    """True when the tables know *word*: an alias key or a family mention."""
    return word in _WRITER_ALIASES or bool(_writer_word_families(word))


#: The first segment of a run id: date/time stamped, as in
#: ``20261024-061038-meta-review-18b0fc`` or ``20261024-1-nvidia-driver-x``.
#: G2: the first date-stamped token ENDS the model/provider position.
_RUN_ID_RE = re.compile(r"^\d{4,8}[-_]")

#: G3: a masked id segment stands in for run-id text -- run-id shaped, so
#: it contributes no model token (``<hex>`` is not a model word).
_MASK_RE = re.compile(r"<(?:hex|uuid)>")


def _model_word_groups(value):
    """Word-bearing tokens of *value*'s model/provider position (G1-G3).

    G1: the position is the whitespace-delimited token(s) before the run
    id; G2: a date-stamped token ENDS it, so text after the run id is slug
    text and never a second writer; G3: a masked id segment is skipped like
    the run-id text it stands in for.  Each surviving token splits into
    words on every non-alphanumeric character (``omniroute/cx-gpt-6.1-sol``
    yields ``omniroute``, ``cx``, ``gpt``, ...), and a token with no word
    at all (``:;,/()[]...---``, ``---``) contributes nothing, keeping a
    purely punctuated value the blank attribution ``writer_unknown``.
    """
    groups = []
    for token in str(value).split():
        if _RUN_ID_RE.match(token):
            break
        if _MASK_RE.search(token):
            continue
        words = [word for word in re.split(r"[^A-Za-z0-9]+",
                                           token.lower()) if word]
        if words:
            groups.append(words)
    return groups


def _writer_value_family(value):
    """The one writer family of a ``Writer:`` value, else ``Refused`` (G4).

    Every word of every model-position token resolves through
    ``_writer_word_families``; the FIRST word of the first word-bearing
    token -- and of any token that names a family -- must be a word the
    tables know, else the value fails closed as ``writer_unknown_family``
    even when a later token resolves: ``kimi-k2 codex`` is not openai, and
    ``run-claude`` never hides behind its ``claude``.  No word-bearing
    token at all keeps the blank-attribution ``writer_unknown``; no family
    after the walk is ``writer_unknown_family``; two families are
    ``writer_mixed_family``.
    """
    groups = _model_word_groups(value)
    if not groups:
        # Punctuation or a bare run id: no model/provider token at all,
        # so the attribution is blank.
        raise Refused("writer_unknown",
                      f"Writer value names no model token: {value!r}")
    families = set()
    for index, words in enumerate(groups):
        named = {family for word in words
                 for family in _writer_word_families(word)}
        if (index == 0 or named) and not _known_word(words[0]):
            raise Refused(
                "writer_unknown_family",
                f"Writer value names a word no alias or family knows: "
                f"{value!r}")
        families |= named
    if not families:
        raise Refused(
            "writer_unknown_family",
            f"Writer value names no known writer family: {value!r}")
    if len(families) > 1:
        raise Refused(
            "writer_mixed_family",
            f"Writer value names several writer families: {value!r}")
    return families.pop()
