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
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def _pos_int(value: object) -> int | None:
    """A real positive int id, else None.

    Shared by both halves of the workflow backfill (workflow-run suite ids and
    check-run suite ids, then the run and workflow ids behind the 'workflow'
    key): bool is not an id (``True == 1`` would hijack suite 1) and a digit
    string is not an id here either — GitHub sends ints.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


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
        When any check-run comes from GitHub Actions, the workflow runs for the
        sha are fetched through the same gated paging and backfilled onto the
        check-runs whose check-suite id matches (``workflow`` key); only a
        positive int suite id counts on either side, and the key is written
        only when the matched run also has a positive int id and workflow_id
        and a string event.
        Raises on a non-zero ``gh`` exit or unparseable output — the verifier
        catches and sanitises that.
        """
        if not valid_commit_ref(sha):
            raise ValueError("check-runs requires a full lowercase commit sha")
        owner, repo = self._validated_slug(self._slug(repo_slug_or_path))
        # Both endpoints are assembled from these validated components only,
        # so neither the slug nor the sha can inject a query or a path segment.
        quoted = quote(owner, safe=""), quote(repo, safe=""), quote(sha, safe="")
        collected = self._page_all(
            "repos/{}/{}/commits/{}/check-runs".format(*quoted),
            "",
            "check_runs",
            "check-runs",
            owner,
            repo,
        )
        # A check-run from GitHub Actions only summarises the workflow run that
        # produced it, so fetch those runs for the sha — but only when such a
        # run exists: no actions check-run, no second ``gh`` call at all.
        if any(
            isinstance(run.get("app"), dict)
            and run["app"].get("slug") == "github-actions"
            for run in collected
        ):
            workflows = self._page_all(
                "repos/{}/{}/actions/runs".format(quoted[0], quoted[1]),
                f"head_sha={quoted[2]}&",
                "workflow_runs",
                "workflow-runs",
                owner,
                repo,
            )
            # First run wins per suite: the API lists newest first, so retries
            # collapse onto the latest attempt. Only a positive int suite id on
            # either side matches (bool excluded), and a matched run adds a
            # 'workflow' key only when its id, workflow_id and str event are
            # all usable — a suite with no usable run simply gets no key.
            by_suite: dict = {}
            for w in workflows:
                suite_id = _pos_int(w.get("check_suite_id"))
                if suite_id is None:
                    continue
                by_suite.setdefault(suite_id, w)
            for run in collected:
                suite = run.get("check_suite")
                suite_id = _pos_int(suite.get("id")) if isinstance(suite, dict) else None
                if suite_id is None:
                    continue
                wf = by_suite.get(suite_id)
                if wf is None:
                    continue
                run_id = _pos_int(wf.get("id"))
                wf_id = _pos_int(wf.get("workflow_id"))
                event = wf.get("event")
                if run_id is None or wf_id is None or not isinstance(event, str):
                    continue
                run["workflow"] = {
                    "event": event,
                    "workflow_id": wf_id,
                    "name": wf.get("name"),
                    "run_id": run_id,
                }
        return collected

    # -- helpers ------------------------------------------------------------
    def _page_all(
        self, base: str, query: str, key: str, label: str, owner: str, repo: str
    ) -> list[dict]:
        """Collect every item under *key* across pages, refusing truncation.

        Shared by the check-runs and the workflow-runs lookups so both carry
        the same contract: at most 10 pages of 100, a non-zero ``gh`` exit
        raises, and a ``total_count`` larger than what was collected fails
        closed instead of returning a partial list. *base* is the API path
        without a query; *query* is any prefix already opened there (empty for
        check-runs, the ``head_sha`` scoping for workflow runs); *label* names
        the list in the truncation error.
        """
        collected: list[dict] = []
        total: int | None = None
        for page in range(1, 11):
            path = f"{base}?{query}per_page=100&page={page}"
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
            page_items = data.get(key) if isinstance(data, dict) else None
            if not isinstance(page_items, list):
                page_items = []
            collected.extend([r for r in page_items if isinstance(r, dict)])
            raw_total = data.get("total_count") if isinstance(data, dict) else None
            if isinstance(raw_total, int) and not isinstance(raw_total, bool):
                total = raw_total
                if len(collected) >= total:
                    break
            if len(page_items) < 100:
                break
        if total is not None and total > len(collected):
            raise RuntimeError(
                f"{label} truncated for {owner}/{repo}: expected {total}, got {len(collected)}"
            )
        return collected

    @staticmethod
    def _validated_slug(slug: object) -> tuple[str, str]:
        if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
            raise ValueError("ci repo must be an owner/repo slug")
        owner, repo = slug.split("/", 1)
        return owner, repo

    def _slug(self, value: str) -> str:
        if _SLUG_RE.fullmatch(value) and not Path(value).is_dir():
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
