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
:mod:`plangraph.review.family`; this module copies neither.  The check fails
closed on any writer it cannot resolve: a commit with no ``Writer`` or
``Hand-Edit`` trailer, an unknown model id, or a git error raises
``Refused('writer_unknown', ...)`` so a closing verdict never rests on a
guess.  ``Hand-Edit`` also counts as ``anthropic`` alongside any ``Writer``: a hand edit
is Claude/operator work, not a model writer.
"""
from __future__ import annotations

import re

from ..review.family import model_family
from .errors import Refused
from .evidence_git import trailer_values

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
            values.extend(trailer_values(value))
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
    both.  Raises :class:`Refused` with code ``writer_unknown`` when any
    evidence commit cannot be resolved to a family.  An item with no commit
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
                    tokens = str(writer).split()
                    # One model and an optional recognised run id, never a list.
                    # Also reject a model-looking second token (even the same
                    # family): it cannot safely stand for a run identifier.
                    if (len(tokens) > 2 or "," in str(writer)
                            or (len(tokens) == 2 and not _run_id(tokens[1]))):
                        raise Refused("writer_unknown",
                                      "Writer value must name one model, not several")
                    model = tokens[0] if tokens else ""
                    segments = model.split("/")
                    # A provider/model pair is allowed, but multiple model
                    # segments (even of one family) are not one writer.
                    resolved = [model_family(part) for part in segments]
                    if (not all(segments) or len(segments) > 2
                            or (len(segments) == 2 and resolved[0] is not None
                                and segments[0].lower() not in ("mimo", "xiaomi-mimo"))):
                        raise Refused("writer_unknown", "ambiguous slash-joined writer")
                    family = model_family(model)
                    if family is None:
                        raise Refused("writer_unknown",
                                      f"unknown writer model {model!r}")
                    families.append(family)
            if _key_values(trailer, "Hand-Edit"):
                families.append("anthropic")
            elif not writers:
                raise Refused(
                    "writer_unknown",
                    f"commit behind {commit} has neither Writer nor Hand-Edit")
    return families


def _run_id(token):
    """Recognise build-L1 run shapes, never a second model attribution.

    Build-L1 override: the second token is a run id only when it resolves to
    NO model family and carries a spawner stamp (``\\d{8}-\\d{6}`` anywhere in
    it), ends ``-<6 hex>`` (autoos title run ids such as
    ``ui-browser-fix3-r2-382049``), or is at least three lowercase
    hyphen-separated segments (``keeper-trailer-multi-fix5``); slashes may
    appear inside the run id (``trailer-multi/20261023-064042-...-0e3d4e``)
    — the slash rule of finding 1 applies to the MODEL token only.  A
    ``ses_<alnum>`` session id also stays valid (finding 2's original spec
    and vector V2; it cannot name a model and the override's refusal list —
    ``run1``, ``kimi-k2``, any 2-segment word — does not cover it).
    ``run1``, ``kimi-k2`` and other bare words are refused.
    """
    if model_family(token) is not None:
        return False
    return bool(re.search(r"[0-9]{8}-[0-9]{6}", token)
                or re.search(r"-[0-9a-f]{6}$", token)
                or re.fullmatch(r"[0-9a-z]+(?:-[0-9a-z]+){2,}", token)
                or re.fullmatch(r"ses_[0-9A-Za-z]+", token))
