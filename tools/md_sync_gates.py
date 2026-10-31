"""md_sync D-row gates: read-only git merge checks and the seat-clause guard.

GitGates checks a Closes key@sha commit against the keeper config's repos (fail
closed on every git error). A registered ``<project>/<track>`` repo is a
fallback for track items (D-3393, the plangraph.keeper.evidence_repo rule):
used only when the primary repo lacks the commit.

names_seats(item): the keeper refuses review_tier 'none' on an item whose
done_criteria names several seats (store_tiers.check_seat_clause_tier), so the
D-row pass must never plan that downgrade (D-3395).
"""
import os
import sys

# Import the keeper from the checkout so tools/ works without an install step.
_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from plangraph.keeper.evidence_git import GitAdapter, resolve_commit  # noqa: E402
from plangraph.keeper.evidence_repo import _TRACK_RE  # noqa: E402
from plangraph.keeper.store_tiers import seat_clause_needs_high  # noqa: E402


class GitGates:
    """Read-only git checks over the keeper config's repos (fail closed)."""

    def __init__(self, repos):
        self._repos = repos or {}
        self._git = GitAdapter()

    def target(self, project):
        """(repo_path|None, ref) for project (both repos shapes)."""
        entry = self._repos.get(project)
        if isinstance(entry, dict):
            path = entry.get("path")
            path = str(path) if isinstance(path, str) and path else None
            ref = entry.get("ref") or "main"
        elif isinstance(entry, str) and entry:
            path, ref = entry, "main"
        else:
            path, ref = None, "main"
        if not (isinstance(ref, str) and ref):
            ref = "main"
        return path, ref

    def check(self, project, token, track=None):
        """{'repo', 'ref', 'full', 'merged'}; never raises.
        The configured ref decides, exactly; a config ref of 'main' or 'origin/main'
        is used as configured, with no fallback to the other.

        With a track, a registered ``<project>/<track>`` repo is a fallback
        (D-3393): it is consulted only when the primary repo lacks the commit,
        so the primary repo wins whenever it holds it.
        """
        primary = self._check(project, token)
        if primary["full"] or not isinstance(track, str) or not _TRACK_RE.fullmatch(track):
            return primary
        key = f"{project}/{track}"
        if self.target(key)[0] is None:
            return primary
        fallback = self._check(key, token)
        return fallback if fallback["full"] else primary

    def _check(self, project, token):
        repo_path, ref = self.target(project)
        full = None
        merged = False
        if repo_path:
            full = resolve_commit(repo_path, token)
            if full:
                try:
                    merged = bool(self._git.is_ancestor(repo_path, full, ref))
                except Exception:  # noqa: BLE001 - fail closed on any git error
                    merged = False
        return {"repo": project if repo_path else None, "ref": ref,
                "full": full, "merged": merged, "path": repo_path}


def track_of(item):
    """The item's track when it is a safe key segment, else None."""
    track = item.get("track")
    if isinstance(track, str) and _TRACK_RE.fullmatch(track):
        return track
    return None


def names_seats(item):
    """The multi-seat clause of the item's done_criteria, else None."""
    done = item.get("done_criteria")
    return seat_clause_needs_high(done if isinstance(done, str) else None)
