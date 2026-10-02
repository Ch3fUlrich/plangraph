"""Real git adapter for the ``commit+ci`` verifier.

Shells out to ``git`` with an argument list (never a shell), a bounded timeout,
and ``cwd`` set to the repository so a relative path can never escape it. Repo
paths are kept per project, from the router-owned ``repos`` config.

Every commit argument is validated as a full lowercase hex sha first, then
passed after ``--end-of-options``; an invalid or option-like value never
reaches git. Commit *dates* are advisory only -- git lets an author forge both
the author and committer timestamps, so the binding evidence is the trailer,
the ancestry of ``main`` and the CI result on that exact sha.
"""

from __future__ import annotations

import subprocess
from typing import Mapping

from .evidence_types import Result, valid_commit_ref

__all__ = ["GitAdapter", "LegacyCommitVerifier", "parse_trailers"]


def parse_trailers(text: str) -> dict[str, str]:
    """Parse ``git show -s --format=%(trailers)`` output into key → value.

    ``%(trailers)`` emits one ``Key: value`` line per trailer (possibly with
    leading blank lines), exactly the shape parsed here. Last value wins.
    """
    trailers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key:
            trailers[key] = value
    return trailers


class GitAdapter:
    """Local ``git`` over a project → repository-path mapping."""

    def __init__(
        self,
        repos: Mapping[str, str] | None = None,
        *,
        git: str = "git",
        timeout: float = 30.0,
    ) -> None:
        self._repos = {str(k): str(v) for k, v in dict(repos or {}).items()}
        self._git = git
        self._timeout = float(timeout)

    # -- config -------------------------------------------------------------
    def repo_path(self, project: str) -> str | None:
        """Return the registered repository path for *project*, or ``None``."""
        path = self._repos.get(project)
        return path if path else None

    def __repr__(self) -> str:  # never dump the whole mapping casually
        return f"GitAdapter(projects={sorted(self._repos)!r})"

    # -- plumbing -----------------------------------------------------------
    def _run(self, repo: str, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._git, *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )

    # -- operations ---------------------------------------------------------
    def commit_exists(self, repo: str, sha: str) -> bool:
        if not valid_commit_ref(sha):
            return False
        proc = self._run(repo, ["cat-file", "-e", "--end-of-options", f"{sha}^{{commit}}"])
        return proc.returncode == 0

    def is_ancestor_of_main(self, repo: str, sha: str) -> bool:
        if not valid_commit_ref(sha):
            return False
        proc = self._run(
            repo, ["merge-base", "--is-ancestor", "--end-of-options", sha, "main"]
        )
        return proc.returncode == 0

    def trailers(self, repo: str, sha: str) -> dict[str, str]:
        if not valid_commit_ref(sha):
            return {}
        proc = self._run(
            repo, ["show", "-s", "--format=%(trailers)", "--end-of-options", sha]
        )
        if proc.returncode != 0:
            return {}
        return parse_trailers(proc.stdout)

    def commit_times(self, repo: str, sha: str) -> tuple[float | None, float | None]:
        """Return ``(author_epoch, committer_epoch)`` or ``(None, None)``.

        Both dates are advisory (forgeable), but the verifier still compares
        both against the claim.
        """
        if not valid_commit_ref(sha):
            return (None, None)
        proc = self._run(
            repo, ["show", "-s", "--format=%at %ct", "--end-of-options", sha]
        )
        if proc.returncode != 0:
            return (None, None)
        parts = proc.stdout.split()
        if len(parts) < 2:
            return (None, None)
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return (None, None)

    def commit_time(self, repo: str, sha: str) -> float | None:
        """The committer date (advisory), or ``None`` when unreadable."""
        return self.commit_times(repo, sha)[1]


class LegacyCommitVerifier:
    """X7b: a commit that exists and is an ancestor of ``main`` — nothing else.

    The legacy import path has no ``Plan-Item`` trailer, no claim-time window and
    no CI run to check, so :class:`~plangraph.keeper.evidence.CommitCiVerifier`
    can never verify its work.  This verifier deliberately asks only the two
    questions a historical commit can still answer, which is precisely why
    attaching kind ``commit`` is router-only (``add_evidence_router``) — see
    ``docs/KEEPER.md``, "Legacy and decision closes".

    It lives beside the adapter it uses, and catches every exception itself: a
    verifier must never raise, and a repo path that does not exist or a git that
    fails for any reason is an unverified result, not a crash.
    """

    kind = "commit"

    def verify(self, item, ref, ctx):
        ref = dict(ref) if isinstance(ref, dict) else {}
        commit = ref.get("commit")
        if not isinstance(commit, str) or not commit:
            return Result(False, "commit requires a 'commit' sha", ref)
        if not valid_commit_ref(commit):
            return Result(False, "commit requires a full lowercase commit sha"
                                 " (40 or 64 hex)", ref)
        project = (item or {}).get("project")
        try:
            repo = ctx.git.repo_path(project) if ctx.git is not None else None
            if not repo:
                return Result(False, f"no repo registered for project"
                                    f" {project!r}", ref)
            if not ctx.git.commit_exists(repo, commit):
                return Result(False, f"commit {commit} does not exist", ref)
            if not ctx.git.is_ancestor_of_main(repo, commit):
                return Result(False, f"commit {commit} is not an ancestor"
                                    f" of main", ref)
        except Exception as exc:  # noqa: BLE001 - a verifier must never raise
            return Result(False, f"git error ({type(exc).__name__})", ref)
        return Result(True, f"commit {commit} is an ancestor of main", ref)
