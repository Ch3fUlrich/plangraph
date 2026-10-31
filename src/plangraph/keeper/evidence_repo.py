"""Track-keyed fallback repo lookup for the commit verifiers.

Rule: a keeper.json ``repos``/``ci`` entry keyed ``<project>/<track>`` is a
FALLBACK repo for items of that project+track -- use it only when the primary
``<project>`` repo lacks the commit and the track repo holds it, never otherwise.
Every adapter error falls back to ``project``: this helper never raises.
"""

from __future__ import annotations

from typing import Any


def repo_key(item: dict, ctx: Any, commit: str) -> str | None:
    """Return ``<project>/<track>`` when only the track repo holds ``commit``.

    Always returns the plain ``project`` key unless the track fallback rule
    applies in full, so the callers' behaviour is unchanged for every item
    without a track or without a registered track repo.
    """
    project = item.get("project")
    try:
        track = item.get("track")
        if not isinstance(project, str) or not project:
            return project
        if not isinstance(track, str) or not track:
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
