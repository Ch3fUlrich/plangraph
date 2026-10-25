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
is Claude/operator work, not a model writer.
"""
from __future__ import annotations

from ..review.family import model_family
from .errors import Refused

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
    try:
        repo = git.repo_path(item["project"])
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise Refused("writer_unknown", "cannot resolve project repository") from exc
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
            writer = trailer.get("Writer")
            if writer:
                model = str(writer).split()[0] if str(writer).split() else ""
                family = model_family(model)
                if family is None:
                    raise Refused("writer_unknown",
                                  f"unknown writer model {model!r}")
            elif trailer.get("Hand-Edit"):
                try:
                    paths = git.changed_paths(repo, trailer.get("_writer_commit", commit))
                except Exception as exc:  # noqa: BLE001 - fail closed
                    raise Refused("writer_unknown", "cannot read hand-edit paths") from exc
                if paths and set(paths) == {"CHANGELOG.md"}:
                    continue
                family = "anthropic"
            else:
                raise Refused(
                    "writer_unknown",
                    f"commit behind {commit} has neither Writer nor Hand-Edit")
            families.append(family)
    return families
