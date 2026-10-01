"""Real git adapter for the ``commit+ci`` verifier.

Shells out to ``git`` with an argument list (never a shell), a bounded timeout,
and ``cwd`` set to the repository so a relative path can never escape it. Repo
paths are kept per project, from the router-owned ``repos`` config.
"""

from __future__ import annotations

import subprocess
from typing import Mapping

__all__ = ["GitAdapter", "parse_trailers"]


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
        if not sha:
            return False
        proc = self._run(repo, ["cat-file", "-e", f"{sha}^{{commit}}"])
        return proc.returncode == 0

    def is_ancestor_of_main(self, repo: str, sha: str) -> bool:
        if not sha:
            return False
        proc = self._run(repo, ["merge-base", "--is-ancestor", sha, "main"])
        return proc.returncode == 0

    def trailers(self, repo: str, sha: str) -> dict[str, str]:
        proc = self._run(repo, ["show", "-s", "--format=%(trailers)", sha])
        if proc.returncode != 0:
            return {}
        return parse_trailers(proc.stdout)

    def commit_time(self, repo: str, sha: str) -> float | None:
        proc = self._run(repo, ["show", "-s", "--format=%ct", sha])
        if proc.returncode != 0:
            return None
        try:
            return float(proc.stdout.strip())
        except ValueError:
            return None
