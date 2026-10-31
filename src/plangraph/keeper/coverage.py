"""Seat coverage: every writer family must be covered by the reviewer seats.

Mirrors ``tools/seat_verdict.coverage`` on the keeper side.  The reviewer
families of the current round are the *seats*; the families named by the
``Writer:`` trailer of every commit behind the item's current-epoch verified
``commit+ci`` evidence are the *writers*.  A writer family is covered when
``need`` distinct seat families other than it exist (``1`` standard, ``2``
high) and, for a sensitive (high) item, at least one of them is neither
``anthropic`` nor the writer itself.  Two Claude seats are therefore one
family, and Claude can never cover a Claude hand on a high item.

Family names (and the one model-id table) come from
:mod:`plangraph.review.family`; this module copies neither.  The check fails
closed on any writer it cannot resolve: a commit with no ``Writer`` or
``Hand-Edit`` trailer, an unknown model id, or a git error raises
``Refused('writer_unknown', ...)`` so a closing verdict never rests on a
guess.  ``Hand-Edit`` with no ``Writer`` counts as ``anthropic``: a hand edit
is Claude/operator work, not a model writer.  A hand edit the adapter proves
to be a changelog-only edit is exempt from that count; that proof
(``changelog_only_edit``) is the single source of truth for the exemption, and
an adapter without it counts the hand.
"""
from __future__ import annotations

from ..review.family import model_family
from .errors import Refused
from .evidence_repo import repo_key

__all__ = ["uncovered", "writer_families"]


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

    Raises :class:`Refused` with code ``writer_unknown`` when any evidence
    commit cannot be resolved to a family.  An item with no commit evidence
    (e.g. decision-only) has no commit writers and returns ``[]``.
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
    families = []
    for row in rows:
        commit = row["ref"]["commit"]
        key = repo_key(item, ctx, commit)
        if not isinstance(key, str) or not key:
            raise Refused("writer_unknown",
                          f"no repo registered for project {key!r}")
        try:
            repo = git.repo_path(key)
        except Exception as exc:  # noqa: BLE001 - fail closed
            msg = ("cannot resolve project repository"
                   if key == item.get("project")
                   else f"cannot resolve repository for {key!r}")
            raise Refused("writer_unknown", msg) from exc
        if not repo:
            raise Refused("writer_unknown",
                          f"no repo registered for project {key!r}")
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
            writer = trailer.get("Writer")
            if writer:
                model = str(writer).split()[0] if str(writer).split() else ""
                family = model_family(model)
                if family is None:
                    raise Refused("writer_unknown",
                                  f"unknown writer model {model!r}")
            elif trailer.get("Hand-Edit"):
                try:
                    leaf = trailer.get("_writer_commit", commit)
                    # ONE source of truth: the adapter's own changelog proof
                    # decides the exemption.  An adapter without it cannot
                    # prove a changelog-only edit, so the hand counts.
                    exempt = getattr(git, "changelog_only_edit", None)
                    is_exempt = bool(exempt(repo, leaf)) if exempt is not None else False
                except Exception as exc:  # noqa: BLE001 - fail closed
                    raise Refused("writer_unknown",
                                  "cannot read the changelog-only proof") from exc
                if is_exempt:
                    continue
                family = "anthropic"
            else:
                raise Refused(
                    "writer_unknown",
                    f"commit behind {commit} has neither Writer nor Hand-Edit")
            families.append(family)
    if not families:
        raise Refused("writer_unknown", "commit evidence has no resolved writers")
    return families
