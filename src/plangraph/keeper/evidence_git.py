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
__all__ = ["GitAdapter", "LegacyCommitVerifier", "parse_trailers", "trailer_lines", "trailer_entries", "plan_items", "valid_ref",
           "resolve_commit", "is_first_parent_merge"]

_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+\Z")
_SHA_ARG_RE = re.compile(r"^[0-9a-fA-F]{7,64}\Z")
_FULL_HEX40_RE = re.compile(r"^[0-9a-f]{40}\Z")


def _stdout(proc: subprocess.CompletedProcess) -> str:
    """Subprocess stdout as text (never assume the subprocess layer's str)."""
    out = proc.stdout
    return out if isinstance(out, str) else out.decode("utf-8", "replace")


def is_first_parent_merge(git: GitAdapter, repo: str, sha: str, ref: str) -> bool | None:
    """Whether *sha* is itself a merge on *ref*'s first-parent chain, or unknown."""
    if not valid_commit_ref(sha) or not valid_ref(ref):
        return None
    try:
        parents = git._run(repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", sha])
        if parents.returncode != 0:
            return None
        fields = _stdout(parents).strip().partition("\n")[0].split()
        if not fields:
            return None
        if len(fields) < 3:
            return False
        chain = git._run(repo, ["rev-list", "--first-parent", "--merges", "--reverse",
                                "--end-of-options", f"{sha}^..{ref}"])
        if chain.returncode != 0:
            return None
        merges = _stdout(chain).strip().splitlines()
        return bool(merges and merges[0].strip().lower() == sha.lower())
    except Exception:
        return None


def valid_ref(ref: Any) -> bool:
    """Validate git ref: only [A-Za-z0-9._/-], no leading '-', no '..'."""
    if not isinstance(ref, str) or not ref:
        return False
    if ref.startswith("-") or ".." in ref:
        return False
    return bool(_REF_RE.fullmatch(ref))


_TRAILER_LINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*:[ \t]*\S")


def trailer_lines(message: str) -> list[str]:
    """The trailer lines of a commit message: its trailing paragraphs made only of ``Key: value`` lines.

    Git reads only the last paragraph, so a ``Plan-Item`` and ``Writer`` paragraph followed by a
    ``Co-Authored-By`` paragraph of its own would read as no trailers at all; both count here.
    The subject paragraph and any prose paragraph end the block.
    """
    paragraphs = re.split(r"\n[ \t]*\n", (message or "").strip("\n"))
    block: list[str] = []
    for paragraph in reversed(paragraphs[1:]):
        lines = paragraph.splitlines()
        if not lines or not all(_TRAILER_LINE.match(line) or (n and line[:1] in " \t")
                                for n, line in enumerate(lines)):
            break
        block[:0] = lines
    return block


def trailer_entries(message: str) -> list[tuple[str, str]]:
    """``(key, value)`` per trailer of *message*; an indented line continues the value before it."""
    entries: list[list[str]] = []
    for line in trailer_lines(message):
        if line[:1] in " \t" and entries:
            entries[-1][1] += " " + line.strip()
        else:
            key, _, value = line.partition(":")
            entries.append([key.strip(), value.strip()])
    return [(key, value) for key, value in entries]


# Include the historical train subjects as well as canonical git revert messages.
_TRAIN_DROP_SUBJECT = "Train [0-9]+: drop([ \t]|$)"
REVERT_GREP = f"^(Revert|{_TRAIN_DROP_SUBJECT}|This reverts commit |Dropped-Part:)"
_TRAIN_DROP = re.compile("^" + _TRAIN_DROP_SUBJECT, re.I)
_REVERT_TARGET = re.compile(r"\bThis reverts commit ([0-9a-f]{7,40})\b", re.I)
_LEGACY_TARGET = re.compile(r"\brevers(?:e|t)\w* (?:[^\n]*?merge )?([0-9a-f]{7,40})\b", re.I)


def delivery_revert(message):
    """A cancellation's target and fallback keys, or None for ordinary work."""
    subject = message.partition("\n")[0]
    drop = _TRAIN_DROP.match(subject)
    target = _REVERT_TARGET.search(message)
    names = {v for k, v in trailer_entries(message) if k.lower() == "dropped-part"}
    if not (drop or target or names or subject.lower().startswith("revert")):
        return None
    names.update(plan_items(message))
    # Old drops named all removals by key, even when the prose cited only one SHA.
    target = target or (drop and not names and _LEGACY_TARGET.search(message))
    return (target[1].lower() if target else None, names)


def plan_items(message: str) -> list[str]:
    """The ``Plan-Item`` values among :func:`trailer_entries` of *message* (a continued value is longer than a key)."""
    return [value for key, value in trailer_entries(message) if key.lower() == "plan-item"]


def parse_trailers(text: str) -> dict[str, str | list[str]]:
    """Parse ``git show -s --format=%(trailers)`` output into key → value.

    ``%(trailers)`` emits one ``Key: value`` line per trailer (possibly with
    leading blank lines), exactly the shape parsed here. Repeated Plan-Item
    values bind a phase merge to each card; other keys retain last-value wins.
    """
    trailers: dict[str, str | list[str]] = {}
    last: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if raw[:1] in " \t" and line and last is not None:  # a continuation of the value before it
            current = trailers[last]
            if isinstance(current, list):
                current[-1] += " " + line
            else:
                trailers[last] = current + " " + line
            continue
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and re.fullmatch(r"[A-Za-z0-9-]+", key):
            if key.lower() == "plan-item":
                previous = next((k for k in trailers if k.lower() == "plan-item"), None)
                if previous is not None:
                    existing = trailers[previous]
                    trailers[previous] = [*(existing if isinstance(existing, list) else [existing]), value]
                    last = previous
                    continue
            trailers[key] = value
            last = key
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
        merge_scan: list[str] | None = None,
    ) -> None:
        self._repos = dict(repos or {})
        self._merge_scan = None if merge_scan is None else list(merge_scan)
        self._git = git
        self._timeout = float(timeout)
        self._clock = clock
        self._fetch_interval = float(fetch_interval)
        self._fetch_lock = threading.Lock()
        self._last_fetch: dict[str, float] = {}

    # -- config -------------------------------------------------------------
    def scan_projects(self) -> list[str]:
        """The projects whose repositories the merged-on-default scan reads (all registered by default)."""
        names = self._repos if self._merge_scan is None else self._merge_scan
        return sorted(name for name in names if name in self._repos)

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

    def _commit_object(self, repo: str, sha: str) -> bool:
        """True when *sha* names a commit OBJECT; a tag never peels to one."""
        proc = self._run(repo, ["cat-file", "-t", "--end-of-options", sha])
        return proc.returncode == 0 and _stdout(proc).strip() == "commit"

    def commit_exists(self, repo: str, sha: str) -> bool:
        if not valid_commit_ref(sha):
            return False
        if self._commit_object(repo, sha):
            return True
        if not self._maybe_fetch(repo):
            return False
        try:
            return self._commit_object(repo, sha)
        except Exception:
            return False

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
            own_merge = is_first_parent_merge(self, repo, sha, ref)
            if own_merge is None:
                return None
            if own_merge:
                m_sha = sha.lower()
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
                out_fp = _stdout(proc_fp)
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
                out_anc = _stdout(proc_anc)
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
            out_email = _stdout(proc_email)
            committer_email = out_email.strip()
            if not committer_email:
                return None

            return (m_sha, committer_email)
        except Exception:
            return None

    def trailers(self, repo: str, sha: str) -> dict[str, str]:
        if not valid_commit_ref(sha) or not self._commit_object(repo, sha):
            return {}
        proc = self._run(repo, ["show", "-s", "--format=%B", "--end-of-options", sha])
        if proc.returncode != 0:
            return {}
        return parse_trailers("\n".join(f"{key}: {value}" for key, value in trailer_entries(proc.stdout)))

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

    def _name_status(self, repo: str, args: list[str]) -> list[tuple[str, str]]:
        proc = self._run(repo, ["diff-tree", "--no-commit-id", "--name-status",
                                "--no-renames", "-r", "-z", *args])
        if proc.returncode != 0:
            raise RuntimeError("cannot read commit changes")
        fields = proc.stdout.split("\0")
        return [(fields[i][:1], fields[i + 1]) for i in range(0, len(fields) - 1, 2)
                if fields[i] and fields[i + 1]]

    def ref_commit(self, repo: str, ref: str) -> str:
        """The commit *ref* names now; raises when git cannot say."""
        if not valid_ref(ref):
            raise ValueError("invalid ref")
        proc = self._run(repo, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"])
        sha = proc.stdout.strip() if proc.returncode == 0 else ""
        if not valid_commit_ref(sha):
            raise RuntimeError("cannot read the ref")
        return sha

    def _on_first_parent_chain(self, repo: str, sha: str, ref: str) -> bool:
        """True when *sha* is on *ref*'s first-parent chain; raises when git cannot say."""
        if not valid_ref(ref):
            raise ValueError("invalid ref")
        listing = self._run(repo, ["rev-list", "--first-parent", "--end-of-options", f"{sha}..{ref}"])
        if listing.returncode != 0:
            raise RuntimeError("cannot read the first-parent chain")
        commits = listing.stdout.split()
        if not commits:  # *ref* is *sha* or behind it
            head = self._run(repo, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"])
            if head.returncode != 0:
                raise RuntimeError("cannot read the ref")
            return head.stdout.strip() == sha
        parents = self._run(repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", commits[-1]])
        if parents.returncode != 0:
            raise RuntimeError("cannot read the oldest chain commit")
        tokens = parents.stdout.split()
        return len(tokens) > 1 and tokens[1] == sha

    def _train_deploy_changes(self, repo, first, tip, names):
        """Read a train's part merge diffs, descending into folded trains."""
        proc = self._run(repo, ["log", "--first-parent", "--merges", "--format=%P %H %s",
                                "--end-of-options", f"{first}..{tip}"])
        if proc.returncode != 0:
            raise RuntimeError("cannot read train parts")
        changes = []
        found = False
        for line in proc.stdout.splitlines():
            match = re.fullmatch(r"([0-9a-f]{40}) ([0-9a-f]{40}) ([0-9a-f]{40}) (.*)", line)
            if not match:
                continue
            parent, part, merge, subject = match.groups()
            if re.match(r"Train [0-9]+: merge (?:train|origin/main)\b", subject, re.I):
                if not re.match(r"Train [0-9]+: merge origin/main\b", subject, re.I):
                    nested, attributed = self._train_deploy_changes(repo, parent, part, names)
                    changes.extend(nested)
                    found |= attributed
                continue
            if names.intersection(self.merge_plan_items(repo, merge)):
                changes.extend(self._name_status(repo, ["--end-of-options", parent, merge]))
                found = True
        return changes, found

    def deploy_changes(self, repo: str, sha: str, ref: str, item_key=None) -> list[tuple[str, str]]:
        """``(status, path)`` pairs the work of *sha* landed on *ref*.

        The commit's own diff plus, when a merge on *ref* brought it in, the diff of
        that merge against its first parent: an untagged follow-up commit of the same
        PR counts as landed work. Raises on any unreadable diff.
        """
        if not valid_commit_ref(sha):
            raise ValueError("invalid commit")
        # A merge commit is read against its first parent only: against the second one every
        # file the default branch gained since the PR branched would look added.
        parents = self._run(repo, ["rev-list", "--parents", "-n", "1",
                                   "--end-of-options", sha])
        tokens = parents.stdout.split() if parents.returncode == 0 else []
        if not tokens or not all(valid_commit_ref(t) for t in tokens):
            raise RuntimeError("cannot read commit parents")
        merge = self.merge_commit_of(repo, sha, ref)
        if merge is None and not self._on_first_parent_chain(repo, sha, ref):
            # Only a direct or squash commit can lack an introducing merge.
            raise RuntimeError("cannot read the merge that brought the commit in")
        landed = []
        if merge and valid_commit_ref(merge[0]):
            proc = self._run(repo, ["show", "-s", "--format=%P%n%s",
                                   "--end-of-options", merge[0]])
            if proc.returncode != 0:
                raise RuntimeError("cannot read merge parents")
            parents, _, subject = proc.stdout.partition("\n")
            landed = parents.split()
            if len(landed) == 2 and all(valid_commit_ref(p) for p in landed):
                if item_key and re.match(r"Landing train [0-9]+\b", subject, re.I):
                    changes, found = self._train_deploy_changes(
                        repo, *landed, {item_key, item_key.rsplit(":", 1)[-1]})
                    if not found:
                        raise RuntimeError("cannot attribute train work to a part")
                    return changes
            else:
                landed = []
        changes = self._name_status(
            repo, ["--end-of-options", tokens[1], sha] if len(tokens) > 1
            else ["--root", "--end-of-options", sha])
        if landed and merge[0] != sha:
            changes += self._name_status(repo, ["--end-of-options", landed[0], merge[0]])
        return changes

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

    def merge_plan_items(self, repo: str, sha: str) -> list[str]:
        """``Plan-Item`` values of every commit *sha* introduced (first parent excluded), nested merges included.

        Only a two-parent merge answers; anything else, or a git failure, answers nothing.
        """
        if not valid_commit_ref(sha) or not self._commit_object(repo, sha):
            return []
        parents = self._run(repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", sha])
        if parents.returncode != 0 or len(_stdout(parents).split()) != 3:
            return []
        first = _stdout(parents).split()[1]
        proc = self._run(repo, ["log", "--format=%B%x1d", "--end-of-options", sha, f"^{first}"])
        if proc.returncode != 0:
            return []
        return [v for record in _stdout(proc).split("\x1d") for v in plan_items(record)]

    def writer_trailers(self, repo: str, sha: str, ref: str | None = None) -> list[dict[str, str]]:
        """Trailer dicts of the commits that authored the change behind *sha*.

        L1 attaches ``commit+ci`` evidence to the PR merge commit, which carries
        only ``Plan-Item``.  For a merge (or a PHASE merge whose second-parent
        range holds inner PR merges) the writers are the NON-merge commits in
        ``merge-base(parent1, parent2)..parent2``: the merge commit itself,
        main-side commits and inner merges are never consulted.  A non-merge
        commit stands for itself.  An octopus merge (3+ parents) has no single
        second-parent range, so it returns no writers at all.  With *ref* a
        two-parent lane head ("merge main into the lane") maps through
        :meth:`merge_commit_of` to its PR merge and takes that merge's range
        (the lane commits, never the main side); a two-parent sha no merged PR
        introduced yields no writers (``ref=None`` keeps the historical range
        behaviour).  Failure returns an empty list: fail-closed writer_unknown.
        """
        if not valid_commit_ref(sha) or not self._commit_object(repo, sha):
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
        if ref is not None:
            merged = self.merge_commit_of(repo, sha, ref)
            if not merged:
                return []
            if merged[0] != sha:
                return self.writer_trailers(repo, merged[0])
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
            target_ref = ctx.git.repo_ref(project)  # required of every adapter
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
