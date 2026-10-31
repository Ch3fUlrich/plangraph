"""Real git adapter for the ``commit+ci`` verifier.

Shells out to ``git`` with an argument list (never a shell), a bounded timeout,
and ``cwd`` set to the repository so a relative path can never escape it. Repo
paths are kept per project, from the router-owned ``repos`` config.

Every commit argument is validated as a full lowercase hex sha first, then
passed after ``--end-of-options``; an invalid or option-like value never
reaches git. Commit *dates* are never used (git lets an author forge both the
author and committer timestamps); the binding evidence is the trailer, the
merged-PR ancestry on the target ref (``merge_commit_of``), the ancestry floor
(``head_sha`` recorded at claim/release) and the CI result on that exact sha.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .evidence_repo import repo_key
from .evidence_types import Result, valid_commit_ref
__all__ = ["GitAdapter", "LegacyCommitVerifier", "parse_trailers", "valid_ref", "resolve_commit"]

_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+\Z")
_SHA_ARG_RE = re.compile(r"^[0-9a-fA-F]{7,64}\Z")
_FULL_HEX40_RE = re.compile(r"^[0-9a-f]{40}\Z")


def valid_ref(ref: Any) -> bool:
    """Validate git ref: only [A-Za-z0-9._/-], no leading '-', no '..'."""
    if not isinstance(ref, str) or not ref:
        return False
    if ref.startswith("-") or ".." in ref:
        return False
    return bool(_REF_RE.fullmatch(ref))


def parse_trailers(text: str) -> dict[str, str | list[str]]:
    """Parse ``git show -s --format=%(trailers)`` output into key → value.

    ``%(trailers)`` emits one ``Key: value`` line per trailer (possibly with
    leading blank lines), exactly the shape parsed here. Repeated Plan-Item
    values bind a phase merge to each card; other keys retain last-value wins.
    """
    trailers: dict[str, str | list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and re.fullmatch(r"[A-Za-z0-9-]+", key):
            if key.lower() == "plan-item":
                previous = next((k for k in trailers if k.lower() == "plan-item"), None)
                if previous is not None:
                    raw = trailers[previous]
                    trailers[previous] = [*(raw if isinstance(raw, list) else [raw]), value]
                    continue
            trailers[key] = value
    return trailers


def resolve_commit(
    repo: str | Path,
    sha: str,
    *,
    git: str = "git",
    timeout: float = 30.0,
) -> str | None:
    """Resolve *sha* to the full lowercase 40-hex commit id via ``git rev-parse``, or None."""
    if not repo or not isinstance(repo, (str, Path)):
        return None
    if not isinstance(sha, str) or sha.startswith("-") or not _SHA_ARG_RE.fullmatch(sha):
        return None
    try:
        proc = subprocess.run(
            [str(git), "rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=float(timeout),
            check=False,
        )
        if proc.returncode != 0:
            return None
        full = proc.stdout.strip().lower()
        if _FULL_HEX40_RE.fullmatch(full):
            return full
        return None
    except Exception:
        return None


class GitAdapter:
    """Local ``git`` over a project → repository-path mapping."""

    def __init__(
        self,
        repos: Mapping[str, Any] | None = None,
        *,
        git: str = "git",
        timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        fetch_interval: float = 60.0,
    ) -> None:
        self._repos = dict(repos or {})
        self._git = git
        self._timeout = float(timeout)
        self._clock = clock
        self._fetch_interval = float(fetch_interval)
        self._fetch_lock = threading.Lock()
        self._last_fetch: dict[str, float] = {}

    # -- config -------------------------------------------------------------
    def repo_path(self, project: str) -> str | None:
        """Return the registered repository path for *project*, or ``None``."""
        entry = self._repos.get(project)
        if isinstance(entry, dict):
            path = entry.get("path")
            return str(path) if path else None
        return str(entry) if entry else None

    def repo_ref(self, project: str) -> str:
        """Return the target ref for *project* (defaults to 'main')."""
        entry = self._repos.get(project)
        if isinstance(entry, dict):
            ref = entry.get("ref")
            if ref and isinstance(ref, str) and valid_ref(ref):
                return ref
        return "main"

    def __repr__(self) -> str:  # never dump the whole mapping casually
        return f"GitAdapter(projects={sorted(self._repos)!r})"

    # -- plumbing -----------------------------------------------------------
    def _run(
        self,
        repo: str,
        args: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._git, *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=self._timeout if timeout is None else float(timeout),
            check=False,
        )

    # -- fetch on demand ----------------------------------------------------
    def _fetch_remote(self, repo: str) -> str | None:
        """Return the remote to fetch for *repo*, or ``None`` when no fetch applies.

        The repo is matched to a registered project by ``repo_path(project) ==
        repo``; only entries whose ref is remote-tracking (contains ``/``) are
        ever fetched, and the remote (text before the first ``/``) must pass
        :func:`valid_ref`.
        """
        try:
            target = str(repo)
        except Exception:
            return None
        ref: str | None = None
        try:
            for project in self._repos:
                if self.repo_path(project) == target:
                    ref = self.repo_ref(project)
                    break
        except Exception:
            return None
        if not ref or "/" not in ref:
            return None
        remote = ref.split("/", 1)[0]
        if not remote or not valid_ref(remote):
            return None
        return remote

    def _maybe_fetch(self, repo: str) -> bool:
        """Fetch the registered remote for *repo* if one is due; True if fetched ok.

        At most one fetch per repo path per ``fetch_interval`` seconds
        (thread-safe, shared by ``commit_exists`` and ``is_ancestor``). Fetch
        failures and timeouts never raise: they count as "no fetch" (False).
        """
        remote = self._fetch_remote(repo)
        if remote is None:
            return False
        try:
            now = float(self._clock())
        except Exception:
            return False
        key = str(repo)
        with self._fetch_lock:
            last = self._last_fetch.get(key)
            if last is not None and (now - last) < self._fetch_interval:
                return False
            self._last_fetch[key] = now
        try:
            proc = self._run(
                repo,
                ["fetch", "--quiet", "--", remote],
                timeout=min(self._timeout, 20.0),
            )
        except Exception:
            return False
        return proc.returncode == 0

    # -- operations ---------------------------------------------------------
    def two_parent_merge_subject(self, repo: str, sha: str) -> str | None:
        """Return the subject of an exact two-parent merge; fail closed otherwise."""
        if not repo or not valid_commit_ref(sha):
            return None
        try:
            proc = self._run(repo, ["show", "--no-show-signature", "-s", "--format=%P%n%s",
                                    "--end-of-options", sha])
            lines = proc.stdout.splitlines()
            if proc.returncode == 0 and len(lines) == 2 and len(lines[0].split()) == 2:
                return lines[1]
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    def commit_exists(self, repo: str, sha: str) -> bool:
        if not valid_commit_ref(sha):
            return False
        proc = self._run(repo, ["cat-file", "-e", "--end-of-options", f"{sha}^{{commit}}"])
        if proc.returncode == 0:
            return True
        if not self._maybe_fetch(repo):
            return False
        try:
            retry = self._run(
                repo, ["cat-file", "-e", "--end-of-options", f"{sha}^{{commit}}"]
            )
        except Exception:
            return False
        return retry.returncode == 0

    def resolve_commit(self, repo: str, sha: str) -> str | None:
        """Resolve *sha* to a full 40-hex commit id, or None."""
        return resolve_commit(repo, sha, git=self._git, timeout=self._timeout)

    def is_ancestor(self, repo: str, sha: str, ref: str) -> bool:
        if not isinstance(sha, str) or not _SHA_ARG_RE.fullmatch(sha) or not valid_ref(ref):
            return False
        proc = self._run(
            repo, ["merge-base", "--is-ancestor", "--end-of-options", sha, ref]
        )
        if proc.returncode == 0:
            return True
        if "/" not in ref:
            return False
        if not self._maybe_fetch(repo):
            return False
        try:
            retry = self._run(
                repo, ["merge-base", "--is-ancestor", "--end-of-options", sha, ref]
            )
        except Exception:
            return False
        return retry.returncode == 0

    def is_ancestor_of_main(self, repo: str, sha: str) -> bool:
        return self.is_ancestor(repo, sha, "main")

    def head_sha(self, repo: str, ref: str) -> str | None:
        """Return the 40-hex lowercase commit SHA of *ref*, or None on failure."""
        if not valid_ref(ref):
            return None
        try:
            proc = self._run(
                repo,
                ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            )
            if proc.returncode != 0:
                return None
            stdout = proc.stdout
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if not isinstance(stdout, str):
                return None
            sha = stdout.strip().lower()
            if not _FULL_HEX40_RE.fullmatch(sha):
                return None
            return sha
        except Exception:
            return None

    def merge_commit_of(
        self, repo: str, sha: str, ref: str
    ) -> tuple[str, str] | None:
        """Find the merge commit on *ref*'s first-parent history that introduced *sha*.

        Uses first-parent git ancestry rather than commit timestamps because git
        dates are forgeable and cannot be trusted for binding provenance evidence.
        """
        if not valid_commit_ref(sha) or not valid_ref(ref):
            return None
        try:
            # Check if sha is itself a merge commit (2+ parents).
            proc_parents = self._run(
                repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", sha]
            )
            if proc_parents.returncode != 0:
                return None
            out_parents = (
                proc_parents.stdout
                if isinstance(proc_parents.stdout, str)
                else proc_parents.stdout.decode("utf-8", "replace")
            )
            lines_parents = [ln.strip() for ln in out_parents.splitlines() if ln.strip()]
            if not lines_parents:
                return None
            tokens = lines_parents[0].split()
            is_merge = len(tokens) >= 3

            if is_merge:
                proc_rev = self._run(
                    repo,
                    [
                        "rev-list",
                        "--first-parent",
                        "--merges",
                        "--reverse",
                        "--end-of-options",
                        f"{sha}^..{ref}",
                    ],
                )
                if proc_rev.returncode != 0:
                    return None
                out_rev = (
                    proc_rev.stdout
                    if isinstance(proc_rev.stdout, str)
                    else proc_rev.stdout.decode("utf-8", "replace")
                )
                rev_lines = [ln.strip().lower() for ln in out_rev.splitlines() if ln.strip()]
                if not rev_lines:
                    return None
                first_line = rev_lines[0]
                if not valid_commit_ref(first_line) or first_line != sha.lower():
                    return None
                m_sha = first_line
            else:
                proc_fp = self._run(
                    repo,
                    [
                        "rev-list",
                        "--first-parent",
                        "--merges",
                        "--reverse",
                        "--end-of-options",
                        f"{sha}..{ref}",
                    ],
                )
                if proc_fp.returncode != 0:
                    return None
                out_fp = (
                    proc_fp.stdout
                    if isinstance(proc_fp.stdout, str)
                    else proc_fp.stdout.decode("utf-8", "replace")
                )
                fp_lines = [ln.strip().lower() for ln in out_fp.splitlines() if ln.strip()]
                fp = [ln for ln in fp_lines if valid_commit_ref(ln)]

                proc_anc = self._run(
                    repo,
                    [
                        "rev-list",
                        "--merges",
                        "--ancestry-path",
                        "--end-of-options",
                        f"{sha}..{ref}",
                    ],
                )
                if proc_anc.returncode != 0:
                    return None
                out_anc = (
                    proc_anc.stdout
                    if isinstance(proc_anc.stdout, str)
                    else proc_anc.stdout.decode("utf-8", "replace")
                )
                anc_lines = [ln.strip().lower() for ln in out_anc.splitlines() if ln.strip()]
                anc = {ln for ln in anc_lines if valid_commit_ref(ln)}

                candidate_m: str | None = None
                for line in fp:
                    if line in anc:
                        candidate_m = line
                        break
                if candidate_m is None or not valid_commit_ref(candidate_m):
                    return None

                proc_m2 = self._run(
                    repo,
                    ["merge-base", "--is-ancestor", "--end-of-options", sha, f"{candidate_m}^2"],
                )
                if proc_m2.returncode != 0:
                    return None

                proc_m1 = self._run(
                    repo,
                    ["merge-base", "--is-ancestor", "--end-of-options", sha, f"{candidate_m}^1"],
                )
                if proc_m1.returncode != 1:
                    return None

                m_sha = candidate_m

            proc_email = self._run(
                repo, ["show", "-s", "--format=%ce", "--end-of-options", m_sha]
            )
            if proc_email.returncode != 0:
                return None
            out_email = (
                proc_email.stdout
                if isinstance(proc_email.stdout, str)
                else proc_email.stdout.decode("utf-8", "replace")
            )
            committer_email = out_email.strip()
            if not committer_email:
                return None

            return (m_sha, committer_email)
        except Exception:
            return None

    def trailers(self, repo: str, sha: str) -> dict[str, str]:
        if not valid_commit_ref(sha):
            return {}
        proc = self._run(
            repo, ["show", "-s", "--format=%(trailers)", "--end-of-options", sha]
        )
        if proc.returncode != 0:
            return {}
        return parse_trailers(proc.stdout)

    def changed_paths(self, repo: str, sha: str) -> list[str]:
        """Exact changed paths (including both sides of renames); raise on error."""
        if not valid_commit_ref(sha):
            raise ValueError("invalid commit")
        proc = self._run(repo, ["diff-tree", "--root", "-m", "--no-commit-id",
                                "--name-only", "--no-renames", "-r", "-z",
                                "--end-of-options", sha])
        if proc.returncode != 0:
            raise RuntimeError("cannot read commit paths")
        return [path for path in proc.stdout.split("\0") if path]

    def changelog_only_edit(self, repo: str, sha: str) -> bool:
        """Prove a non-merge addition/modification of the root changelog."""
        if not valid_commit_ref(sha):
            raise ValueError("invalid commit")
        parents = self._run(repo, ["rev-list", "--parents", "-n", "1",
                                   "--end-of-options", sha])
        if parents.returncode != 0:
            raise RuntimeError("cannot read commit parents")
        if len(parents.stdout.split()) > 2:
            return False
        proc = self._run(repo, ["diff-tree", "--root", "--no-commit-id",
                                "--name-status", "--no-renames", "-r", "-z",
                                "--end-of-options", sha])
        if proc.returncode != 0:
            raise RuntimeError("cannot read commit status")
        return proc.stdout.split("\0") == ["M", "CHANGELOG.md", ""] or proc.stdout.split("\0") == ["A", "CHANGELOG.md", ""]

    def _writer_trailer(self, repo, sha):
        trailers = self.trailers(repo, sha)
        if trailers.get("Hand-Edit") and not trailers.get("Writer"):
            trailers = dict(trailers, _writer_commit=sha)
        return trailers

    def writer_trailers(self, repo: str, sha: str) -> list[dict[str, str]]:
        """Trailer dicts of the commits that authored the change behind *sha*.

        L1 attaches ``commit+ci`` evidence to the PR merge commit, which carries
        only ``Plan-Item``.  For a merge (or a PHASE merge whose second-parent
        range holds inner PR merges) the writers are the NON-merge commits in
        ``merge-base(parent1, parent2)..parent2``: the merge commit itself,
        main-side commits and inner merges are never consulted.  A non-merge
        commit stands for itself.  An octopus merge (3+ parents) has no single
        second-parent range, so it returns no writers at all.  Failure returns
        an empty list, which the keeper treats as fail-closed
        ``writer_unknown``.
        """
        if not valid_commit_ref(sha):
            return []
        proc = self._run(
            repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", sha]
        )
        if proc.returncode != 0:
            return []
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            return []
        tokens = lines[0].split()
        if not tokens:
            return []
        parents = tokens[1:]
        if len(parents) > 2:
            # Octopus merge: parents[2:] have no range to derive writers from,
            # so refusing ([] → writer_unknown) is safer than silently
            # ignoring the commits they carry.
            return []
        if len(parents) < 2:
            return [self._writer_trailer(repo, sha)]
        base = self._run(
            repo, ["merge-base", "--end-of-options", parents[0], parents[1]]
        )
        base_sha = base.stdout.strip().lower() if base.returncode == 0 else ""
        if not valid_commit_ref(base_sha):
            return []
        rev = self._run(
            repo,
            ["rev-list", "--no-merges", "--end-of-options",
             f"{base_sha}..{parents[1]}"],
        )
        if rev.returncode != 0:
            return []
        out: list[dict[str, str]] = []
        for line in rev.stdout.splitlines():
            commit = line.strip().lower()
            if not valid_commit_ref(commit):
                continue
            out.append(self._writer_trailer(repo, commit))
        return out

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
        commit = ref.get("sha") or ref.get("commit")
        if not isinstance(commit, str) or not commit:
            return Result(False, "commit requires a 'commit' sha", ref)
        if not valid_commit_ref(commit):
            return Result(False, "commit requires a full lowercase commit sha"
                                 " (40 or 64 hex)", ref)
        project = repo_key(item or {}, ctx, commit)
        try:
            repo = ctx.git.repo_path(project) if ctx.git is not None else None
            if not repo:
                return Result(False, f"no repo registered for project"
                                    f" {project!r}", ref)
            if not ctx.git.commit_exists(repo, commit):
                return Result(False, f"commit {commit} does not exist", ref)
            target_ref = ctx.git.repo_ref(project) if hasattr(ctx.git, "repo_ref") else "main"
            if hasattr(ctx.git, "is_ancestor"):
                is_anc = ctx.git.is_ancestor(repo, commit, target_ref)
            else:
                is_anc = ctx.git.is_ancestor_of_main(repo, commit)
            if not is_anc:
                return Result(False, f"commit {commit} is not an ancestor"
                                    f" of {target_ref}", ref)
        except Exception as exc:  # noqa: BLE001 - a verifier must never raise
            return Result(False, f"git error ({type(exc).__name__})", ref)
        return Result(True, f"commit {commit} is an ancestor of {target_ref}", ref)
