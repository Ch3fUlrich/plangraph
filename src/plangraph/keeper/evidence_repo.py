"""Track-keyed fallback repo lookup for the commit verifiers.

Rule: a keeper.json ``repos``/``ci`` entry keyed ``<project>/<track>`` is a
FALLBACK repo for items of that project+track -- use it only when the primary
``<project>`` repo lacks the commit and the track repo holds it, never otherwise.
Every adapter error falls back to ``project``: this helper never raises.
A fallback commit with ``bind_floor_required`` set fails closed when the recorded
floor sha is unknown to the track repo on purpose: the floor is recorded from the
primary repo, and a floor only exists after a release or a re-claim by another
principal, so refusing is the intended, safe behaviour.
"""

from __future__ import annotations

import re
from typing import Any

#: A safe project key: no ``/``, so ``<project>/<track>`` cannot collide with
#: another project's track key (``a/b`` + ``c`` vs ``a`` + ``b/c``).
_PROJECT_RE = re.compile(r"[A-Za-z0-9._-]+")
#: A safe track key: no ``/``, 1..64 characters.
_TRACK_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def track_key(item: Any) -> str | None:
    """Return ``<project>/<track>`` when both match the safe regexes, else None.

    Returns None if *item* is not a dict or if either field is missing, not a string,
    or does not match its regex. Never raises.
    """
    if not isinstance(item, dict):
        return None
    try:
        project = item.get("project")
        track = item.get("track")
        if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
            return None
        if not isinstance(track, str) or not _TRACK_RE.fullmatch(track):
            return None
        return f"{project}/{track}"
    except Exception:  # noqa: BLE001
        return None


def repo_key(item: dict, ctx: Any, commit: str) -> str | None:
    """Return ``<project>/<track>`` when only the track repo holds ``commit``.

    Always returns the plain ``project`` key unless the track fallback rule
    applies in full, so the callers' behaviour is unchanged for every item
    without a track or without a registered track repo.  A non-dict *item*
    yields ``None``; no input type can make this helper raise.
    """
    if not isinstance(item, dict):
        return None
    project = None
    try:
        project = item.get("project")
        track = item.get("track")
        if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
            return project
        if not isinstance(track, str) or not _TRACK_RE.fullmatch(track):
            return project
        key = f"{project}/{track}"
        git = getattr(ctx, "git", None)
        if git is None:
            return project
        track_repo = git.repo_path(key)
        if not track_repo:
            return project
        primary = git.repo_path(project)
        if primary and git.commit_exists(primary, commit):
            return project
        if git.commit_exists(track_repo, commit):
            return key
        return project
    except Exception:  # noqa: BLE001 - fail to the old behaviour, never raise
        return project
