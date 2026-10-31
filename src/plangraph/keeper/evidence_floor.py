"""Per-repo ancestry floors for the keeper evidence engine (D-3335).

A floor event -- the item's latest ``released`` event, or ``claimed`` event by
another principal -- records ``base`` (the primary repo head at that instant)
and ``bases``: ``{repo_key: head_sha}`` for the primary key and, when the item
has a track and a ``<project>/<track>`` repo is configured, for that track key
too.  A commit that resolves to the track repo
(:func:`plangraph.keeper.evidence_repo.repo_key`) is floored against that
repo's own entry; a floor event predating ``bases`` (legacy, or the repo had
no head) holds no track floor and the floor check is skipped for that repo
only.  A malformed ``bases`` payload fails closed.

Resolution rules (:func:`check_floor`):

* ``bind_floor_shas is None`` -- a context from before per-repo floors: the
  single ``bind_floor_sha`` applies to every repo, exactly as before.
* primary key -- ``bind_floor_sha``, unchanged, failing closed when unset.
* track key -- ``bases[track_key]``; a missing entry means no floor for that
  repo (the check is skipped); a malformed map or entry refuses.
"""
from __future__ import annotations

import re
from typing import Any

from .evidence_types import Result, valid_commit_ref

__all__ = ["ref_head", "record_bases", "event_bases", "check_floor"]

#: The refusal family of the ancestry-floor check (unchanged wording).
_FLOOR_REFUSAL = (
    "no ancestry floor recorded for this item's last release/re-claim; refusing"
)

#: Marker: the floor event's ``bases`` payload cannot be trusted.
_MALFORMED = object()

#: Mirrors evidence_repo's key shapes so a recorded track key is exactly the
#: key ``repo_key`` can resolve to (no ``/`` in a project, 1..64 track chars).
_PROJECT_RE = re.compile(r"[A-Za-z0-9._-]+")
_TRACK_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


# --------------------------------------------------------------------------- #
# recording (claim/release payloads)
# --------------------------------------------------------------------------- #

def ref_head(git: Any, project: str) -> str | None:
    """Best-effort head SHA of *project*'s target ref, or ``None``.

    This is the lookup the store's ``_ref_head`` has always done: the git
    adapter's ``repo_path``/``repo_ref``/``head_sha``.  An unregistered repo,
    an unknown head and any adapter failure all yield ``None``; never raises.
    """
    try:
        if git is None:
            return None
        head_sha_fn = getattr(git, "head_sha", None)
        if not callable(head_sha_fn):
            return None
        repo_path_fn = getattr(git, "repo_path", None)
        if not callable(repo_path_fn):
            return None
        repo = repo_path_fn(project)
        if not repo:
            return None
        repo_ref_fn = getattr(git, "repo_ref", None)
        ref = repo_ref_fn(project) if callable(repo_ref_fn) else "main"
        if not ref:
            ref = "main"
        sha = head_sha_fn(repo, ref)
        if isinstance(sha, str) and valid_commit_ref(sha) and sha == sha.lower():
            return sha
        return None
    except Exception:  # noqa: BLE001 - an unknown head is just unknown
        return None


def record_bases(item: Any, git: Any) -> dict:
    """The ``bases`` map to record on a claim/release floor event.

    ``{repo_key: head_sha}`` for the primary key (the item's project) and,
    when the item has a track and a ``<project>/<track>`` repo is configured,
    for that track key too.  A key whose head is unknown (or whose repo is not
    configured) is skipped, so the map may be empty.  The store records the map
    only when non-empty -- exactly like ``base``, so a payload with nothing new
    to say stays byte-identical to a pre-change one.  Never raises.
    """
    keys: list[str] = []
    project = item.get("project") if isinstance(item, dict) else None
    if isinstance(project, str) and _PROJECT_RE.fullmatch(project):
        keys.append(project)
        track = item.get("track")
        if isinstance(track, str) and _TRACK_RE.fullmatch(track):
            keys.append(f"{project}/{track}")
    bases: dict = {}
    for key in keys:
        sha = ref_head(git, key)
        if sha is not None:
            bases[key] = sha
    return bases


# --------------------------------------------------------------------------- #
# floor-event parsing and per-repo resolution
# --------------------------------------------------------------------------- #

def event_bases(payload: Any) -> Any:
    """The floor event's ``bases`` value for :class:`Context`.

    ``{}`` for a legacy event (recorded before per-repo floors) and the raw
    ``bases`` value otherwise; a malformed value (non-dict, or any entry that
    is not a lowercase 40/64 hex sha) passes through as-is -- or as
    ``_MALFORMED`` when it cannot -- so :func:`check_floor` fails closed on it.
    """
    if not isinstance(payload, dict):
        return _MALFORMED
    if "bases" not in payload:
        return {}
    bases = payload.get("bases")
    return _MALFORMED if bases is None else bases


def _valid_bases(shas: Any) -> bool:
    """True for a well-formed ``bases`` map: str keys mapping to shas."""
    return isinstance(shas, dict) and all(
        isinstance(key, str) and valid_commit_ref(value)
        for key, value in shas.items()
    )


def check_floor(ctx: Any, repo: Any, commit: str, ref: dict,
                project_key: Any, item: Any = None) -> Result | None:
    """Run the ancestry-floor check for the repo *commit* resolved to.

    ``project_key`` is the ``repo_key`` result: the item's own project key is
    floored by ``bind_floor_sha`` (unchanged), a ``<project>/<track>`` key by
    that repo's entry in ``bind_floor_shas``.  Returns ``None`` when the
    verification should continue (the check passed, or no floor applies to
    this repo) and the refusal :class:`Result` otherwise.
    """
    if not getattr(ctx, "bind_floor_required", True):
        return None
    shas = getattr(ctx, "bind_floor_shas", None)
    floor = getattr(ctx, "bind_floor_sha", None)
    primary = item.get("project") if isinstance(item, dict) else None
    track_key = (
        project_key if isinstance(project_key, str) and "/" in project_key
        and project_key != primary else None
    )
    if shas is not None:
        if not _valid_bases(shas):
            return Result(False, _FLOOR_REFUSAL, ref)  # malformed: fail closed
        if track_key is not None:
            if track_key not in shas:
                return None  # no floor for that repo
            floor = shas[track_key]
    if not isinstance(floor, str) or not valid_commit_ref(floor):
        return Result(False, _FLOOR_REFUSAL, ref)
    is_ancestor_fn = getattr(getattr(ctx, "git", None), "is_ancestor", None)
    if is_ancestor_fn is None:
        return Result(False, "git adapter cannot check ancestry floor; failing closed", ref)
    try:
        is_floor_anc = is_ancestor_fn(repo, floor, commit)
    except Exception as exc:  # noqa: BLE001 - adapter failure is a verification failure
        return Result(
            False, f"git error checking the ancestry floor ({type(exc).__name__})", ref)
    if not is_floor_anc:
        return Result(
            False,
            f"commit {commit} does not descend from the item's last release/re-claim ({floor[:12]})",
            ref,
        )
    return None
