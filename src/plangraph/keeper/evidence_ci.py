"""Real CI adapter: wraps ``gh api .../check-runs`` for the ``commit+ci`` verifier.

The per-project ``ci`` config maps a project to an ``owner/repo`` slug, a local
repository path, or the literal ``none`` (the project has no CI, so the CI half
of ``commit+ci`` is satisfied without a lookup). An absent entry is *not*
``none`` — it fails closed in the verifier.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

from .evidence_types import valid_commit_ref

__all__ = ["CIAdapter"]

# Only a strict owner/repo slug may reach the ``gh api`` path; every other
# character (query, fragment, extra slash, space, dot-segments) is rejected.
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def slug_from_remote(url: str) -> str | None:
    """Turn a git remote URL into ``owner/repo`` (``None`` if unrecognisable)."""
    url = url.strip()
    if not url:
        return None
    if url.startswith("git@") and ":" in url:
        url = url.split(":", 1)[1]
    elif "://" in url:
        url = url.split("://", 1)[1]
        url = url.split("/", 1)[1] if "/" in url else ""
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = url.split("/")
    if len(parts) >= 2 and all(parts[-2:]):
        return f"{parts[-2]}/{parts[-1]}"
    return None


class CIAdapter:
    """Injected through ``ctx.ci``; tests substitute a fake with the same shape."""

    def __init__(
        self,
        ci: Mapping[str, str] | None = None,
        *,
        gh: str = "gh",
        git: str = "git",
        timeout: float = 60.0,
    ) -> None:
        self._ci = {str(k): str(v) for k, v in dict(ci or {}).items()}
        self._gh = gh
        self._git = git
        self._timeout = float(timeout)

    def mode(self, project: str) -> str | None:
        """Return the configured slug/path, ``"none"``, or ``None`` if unset."""
        value = self._ci.get(project)
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        return "none" if value.lower() == "none" else value

    def check_runs(self, repo_slug_or_path: str, sha: str) -> list[dict]:
        """Return the normalised list of check-runs for *sha*.

        Accepts an ``owner/repo`` slug or a local checkout path; a path is turned
        into a slug through its ``origin`` remote. The sha and the slug are both
        validated before any process runs; the API path is assembled from those
        validated components only, so no user text can inject a query.
        Raises on a non-zero ``gh`` exit or unparseable output — the verifier
        catches and sanitises that.
        """
        if not valid_commit_ref(sha):
            raise ValueError("check-runs requires a full lowercase commit sha")
        owner, repo = self._validated_slug(self._slug(repo_slug_or_path))
        path = "repos/{}/{}/commits/{}/check-runs".format(
            quote(owner, safe=""), quote(repo, safe=""), quote(sha, safe="")
        )
        proc = subprocess.run(
            [self._gh, "api", path],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh api exited {proc.returncode} for {owner}/{repo}")
        data = json.loads(proc.stdout)
        runs = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            return []
        return [r for r in runs if isinstance(r, dict)]

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _validated_slug(slug: object) -> tuple[str, str]:
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise ValueError("ci repo must be an owner/repo slug")
        owner, repo = slug.split("/", 1)
        return owner, repo

    def _slug(self, value: str) -> str:
        if _SLUG_RE.match(value) and not Path(value).is_dir():
            return value
        path = Path(value)
        if path.is_dir():
            proc = subprocess.run(
                [self._git, "-C", value, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
            if proc.returncode == 0:
                derived = slug_from_remote(proc.stdout)
                if derived:
                    return derived
        return value

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return f"CIAdapter(projects={sorted(self._ci)!r})"
