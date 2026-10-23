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

from .evidence_types import Result, valid_commit_ref

__all__ = ["GitAdapter", "LegacyCommitVerifier", "parse_trailers", "trailer_values",
           "valid_ref", "resolve_commit"]

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


def _is_trailer_line(raw: str) -> bool:
    """True for a non-indented ``Token: value`` line whose ``Token`` has no space.

    A line whose key holds a space (``Note to reviewer: see the plan``) is
    prose that merely contains a colon, never a git trailer.
    """
    if raw[:1] in (" ", "\t") or ":" not in raw:
        return False
    key = raw.partition(":")[0]
    return bool(key) and not any(char.isspace() for char in key)


def _paragraph_is_trailer_block(lines: list[str]) -> bool:
    """True when EVERY line of one paragraph is a trailer or a continuation.

    An indented line continues the trailer before it, so it only counts when
    a trailer line precedes it in the same paragraph; a line that is neither
    makes the whole paragraph prose.
    """
    if not lines:
        return False
    after_trailer = False
    for raw in lines:
        if raw[:1] in (" ", "\t"):
            if not after_trailer:
                return False
            continue
        if not _is_trailer_line(raw):
            return False
        after_trailer = True
    return True


def _trailing_trailer_start(text: str) -> int | None:
    """The line index where *text*'s trailing trailer block begins, or None.

    The block is the maximal run of final paragraphs in which every non-blank
    line is a trailer (or a continuation of one).  ``None`` means *text* has
    no trailing trailer block at all.
    """
    lines = text.splitlines()
    paragraphs: list[tuple[int, list[str]]] = []
    current: list[str] = []
    begin = 0
    for index, raw in enumerate(lines):
        if raw.strip():
            if not current:
                begin = index
            current.append(raw)
        elif current:
            paragraphs.append((begin, current))
            current = []
    if current:
        paragraphs.append((begin, current))
    start = len(paragraphs)
    while start and _paragraph_is_trailer_block(paragraphs[start - 1][1]):
        start -= 1
    if start == len(paragraphs):
        return None
    return paragraphs[start][0]


def _trailing_trailer_lines(text: str) -> list[str]:
    """The lines of *text*'s TRAILING trailer block (``[]`` when it has none).

    Only that run is read: a prose paragraph ENDS the block, so a
    ``Hand-Edit:``/``Writer:``/``Plan-Item:`` line quoted earlier in the body
    is prose, not a trailer, and never reaches the item a commit is attached
    to.  Trailing blank lines are ignored, and paragraphs keep their original
    spacing so a continuation still joins the trailer before the blank line
    that follows it.
    """
    start = _trailing_trailer_start(text)
    if start is None:
        return []
    return text.splitlines()[start:]


#: A line that names a writer the way a human writes one: the key ``writer``
#: (case-insensitive, as git's trailer keys are), any non-alphanumeric
#: decoration around it, a colon, and a non-space value.  Deliberately LOOSE
#: and unanchored — it spells out no list of shapes — so ``**Writer:** x``,
#: ``1. Writer: x``, ```Writer: x` ``, ``| Writer: x |``, ``-Writer: x``,
#: ``- Writer: x``, ``> Writer: x``, ``    Writer: x`` and ``Writer : x`` all
#: match, while the prose "the writer of this" (no colon) does not.
_WRITER_SHAPED_RE = re.compile(
    r"writer[^A-Za-z0-9]*[:\uff1a\ua789\u2236][ \t]*\S", re.IGNORECASE)

#: A KEY that only *looks* like ``writer``: decoration glued around it
#: (``-Writer``, ``**Writer``, `` `Writer ``).  Such a key parses as its own
#: trailer key, so ``parse_trailers`` keeps the value under ``-Writer`` (say)
#: and drops it from the writer set — a partial family list again.
_WRITER_DECORATED_KEY_RE = re.compile(
    r"^[^A-Za-z0-9]*writer[^A-Za-z0-9]*$", re.IGNORECASE)


def _writer_like_key(key: str) -> bool:
    """True when *key* looks like the git trailer key ``writer``.

    Covers both spellings a dropped writer key hides behind: decoration glued
    around the word (``-Writer``, ``**Writer``, `` `Writer ``) and a key that
    merely starts with it (``Writers``, ``Writer-Notes``).  The exact key
    ``writer`` is included — callers decide whether that one is readable.
    """
    key = key.strip()
    return key.lower().startswith("writer") or bool(
        _WRITER_DECORATED_KEY_RE.match(key))


def _writer_named(raw: str) -> bool:
    """True when *raw* names a writer instead of merely mentioning writing.

    Either the loose Writer-shaped pattern (a ``writer`` key, decorated or
    not, followed by ``:`` and a non-space value) or a line whose KEY only
    looks like ``writer`` — its value is never read as a writer either way.
    """
    if _WRITER_SHAPED_RE.search(raw):
        return True
    return ":" in raw and _writer_like_key(raw.partition(":")[0])


def stray_writer_line(text: str) -> bool:
    """True when a Writer-shaped line sits OUTSIDE the trailing trailer block.

    ``parse_trailers`` reads only the trailing block, so a ``Writer:`` line
    in an earlier trailer-shaped paragraph — or quoted in a prose paragraph —
    is never returned.  For the *Plan-Item* binding that is right (a prose
    quote must not re-attach a commit), but for the writer families it is
    ambiguous: the dropped line may name a writer that no reviewer seat
    covers.  The keeper must fail closed instead of closing on a PARTIAL
    family list, so ANY Writer-shaped line outside the trailing block makes
    the writer set unknowable.  The test is the loose :data:`_WRITER_SHAPED_RE`
    pattern, not a list of spellings: indented (``    Writer: x``), quoted
    (``> Writer: x``, ``>> Writer: x``), bulleted (``- Writer: x``,
    ``* Writer: x``), marker-glued (``-Writer: x``), spaced-before-colon
    (``Writer : x``), markdown (``**Writer:** x``), numbered
    (``1. Writer: x``), backticked (``` `Writer: x` ```), tabular
    (``| Writer: x |``) and any other decoration all count, while prose that
    only uses the word "writer" (no colon) does not.
    """
    start = _trailing_trailer_start(text)
    lines = text.splitlines()
    outside = lines if start is None else lines[:start]
    return any(_writer_named(raw) for raw in outside)


def hidden_writer_line(text: str) -> bool:
    """True when the TRAILING trailer block itself hides a writer line.

    Two ways a block can look complete while dropping a writer, both refused
    here:

    * a Writer-shaped line that is NOT the plain ``Writer: x`` trailer of
      the block: ``**Writer:** x``, ``-Writer: x`` and ``` `Writer: x` ```
      are trailer-shaped lines, so the paragraph JOINS the trailing block
      and the value is parsed under the junk key ``-Writer``; the same is
      true of a writer-looking key that is not the git key (``Writers: x``)
      and of a Writer-shaped value under any other key (``Reviewed-By:
      Writer: x``).  None of them reaches the writer set, so each refuses;
    * a CONTINUATION (leading space/tab), which ``parse_trailers`` folds onto
      the trailer before it: a continuation that is itself Writer-shaped (V1)
      hides a whole ``Writer:`` line, and any continuation folded onto a
      ``Writer`` key (V3) turns two models into one value the family rule
      reads a single token from.

    A continuation that is neither writer-shaped nor attached to a ``Writer``
    key — a ``Plan-Item`` note, say — is left alone, and so is a plain
    ``Writer: x`` trailer line of the block.
    """
    start = _trailing_trailer_start(text)
    if start is None:
        return False
    last: str | None = None
    for raw in text.splitlines()[start:]:
        if not raw.strip():
            last = None
            continue
        if raw[:1] in (" ", "\t"):
            if _writer_named(raw):
                return True
            if last is not None and last.lower() == "writer":
                return True
            continue
        line = raw.strip()
        if not line or ":" not in line:
            last = None
            continue
        key = line.partition(":")[0].strip()
        if not key:
            last = None
            continue
        if key.lower() != "writer" and _writer_named(raw):
            # `**Writer:** x` / `-Writer: x` / ```Writer: x``` parse under a
            # junk key, `Writers: x` is not the git key either, and a
            # Writer-shaped value under any other key (`Reviewed-By: Writer:
            # x`) is kept under a key the seat-coverage check never reads —
            # each of them would leave the family list short a writer.
            return True
        last = key
    return False


def parse_trailers(text: str) -> dict[str, str | list[str]]:
    """Parse the TRAILING trailer block of *text* into key → value(s), in order.

    Only the maximal run of final paragraphs whose every non-blank line is a
    ``Token: value`` trailer (or an indented continuation of one) is read —
    the same paragraphs git's ``%(trailers)`` considers a trailer block, minus
    its "last paragraph only" limit.  A prose paragraph anywhere after them
    ends the block: a body line such as ``Writer: ...`` or ``Plan-Item: ...``
    inside prose is quoting text, not carrying a trailer, and is dropped
    rather than allowed to fake a writer family or re-attach the commit.

    A key that occurs once keeps its plain ``str`` value, so a one-value
    lookup (``Plan-Item``) sees exactly what it saw before.  A key that
    occurs MORE than once keeps every value as a ``list[str]`` in message
    order: a squashed commit may carry several ``Writer:`` lines, and the
    seat-coverage check must see every writer family, not only the last
    (or the first) one.

    An indented line continues the trailer before it (git's own continuation
    rule), which ``%(trailers)`` used to unfold for us; a blank line ends a
    paragraph, so nothing continues across one.  That fold is harmless for a
    ``Plan-Item`` note, but it is never a writer list: a continuation that
    touches a ``Writer`` key is refused one level up (``writer_trailers``
    returns no writers at all rather than one value naming two models, see
    :func:`hidden_writer_line`).
    """
    values: dict[str, list[str]] = {}
    last: str | None = None
    for raw in _trailing_trailer_lines(text):
        if raw[:1] in (" ", "\t") and last is not None and values[last]:
            values[last][-1] = f"{values[last][-1]} {raw.strip()}".strip()
            continue
        line = raw.strip()
        if not line or ":" not in line:
            last = None
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            last = None
            continue
        values.setdefault(key, []).append(value)
        last = key
    return {key: seen[0] if len(seen) == 1 else seen
            for key, seen in values.items()}


def trailer_values(value: Any) -> list[str]:
    """Every value of one parsed trailer key: a ``str`` is a single value.

    Lets a consumer (the seat-coverage check) treat both shapes — the plain
    string a single ``Writer:``/``Hand-Edit:`` line yields and the list a
    repeated key yields — the same way.  Blank values are dropped: a blank
    trailer line names nothing.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = [str(value)]
    return [item for item in items if item.strip()]


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

    def _commit_body(self, repo: str, sha: str) -> str | None:
        """The commit *sha*'s message body, or None when git cannot read it."""
        if not valid_commit_ref(sha):
            return None
        proc = self._run(
            repo, ["show", "-s", "--format=%b", "--end-of-options", sha]
        )
        if proc.returncode != 0:
            return None
        return proc.stdout

    def _leaf_trailers(self, repo: str, sha: str) -> dict[str, str | list[str]] | None:
        """Trailers of ONE writer-bearing commit; None when it is unknowable.

        None covers both a git failure and a writer line the parse cannot
        return whole: :func:`stray_writer_line` (a Writer-shaped line outside
        the trailing block) or :func:`hidden_writer_line` (a writer-shaped
        junk key, or a continuation folded into a ``Writer`` value, inside
        it).  In every one of those cases the writer families cannot be read
        without guessing, so the caller fails closed instead of handing the
        seat-coverage check a partial family list.
        """
        body = self._commit_body(repo, sha)
        # Scan the subject too, but never parse it as a trailer: even a
        # trailer-shaped subject is not part of git's body trailer block.
        message = self._run(
            repo, ["show", "-s", "--format=%B", "--end-of-options", sha]
        )
        subject = message.stdout.split("\n", 1)[0]
        if (body is None or message.returncode != 0 or _writer_named(subject)
                or stray_writer_line(message.stdout) or hidden_writer_line(body)):
            return None
        return parse_trailers(body)

    def trailers(self, repo: str, sha: str) -> dict[str, str | list[str]]:
        """Every trailer of the commit *sha*'s message, key → value(s).

        The message BODY (``%b``) is parsed, not git's ``%(trailers)``
        placeholder: that placeholder reports only the LAST paragraph of the
        message, so a ``Writer:`` block (or a ``Plan-Item:`` line) followed
        by a blank line and another paragraph — e.g. ``Co-Authored-By`` —
        was silently dropped, hiding a writer from the seat-coverage check.
        git never takes the subject for a trailer block (``%(trailers)`` is
        empty for a subject-only message), so reading the body reports
        everything the placeholder did plus the paragraphs it dropped.

        Only the body's TRAILING trailer block counts (see
        :func:`parse_trailers`): a prose paragraph ends it, so a
        ``Hand-Edit:``/``Writer:``/``Plan-Item:`` line quoted in prose is
        never returned as a trailer.
        """
        body = self._commit_body(repo, sha)
        if body is None:
            return {}
        return parse_trailers(body)

    def writer_trailers(self, repo: str, sha: str) -> list[dict[str, str | list[str]]]:
        """Trailer dicts of the commits that authored the change behind *sha*.

        L1 attaches ``commit+ci`` evidence to the PR merge commit, which carries
        only ``Plan-Item``.  For a merge (or a PHASE merge whose second-parent
        range holds inner PR merges) the writers are the NON-merge commits in
        ``merge-base(parent1, parent2)..parent2``: the merge commit itself,
        main-side commits and inner merges are never consulted.  A non-merge
        commit stands for itself.  Every ``Writer:`` value of such a commit is
        kept (a squashed leaf may name several writer families).  An octopus
        merge (3+ parents) has no single second-parent range, so it returns no
        writers at all.  Failure returns an empty list, which the keeper treats
        as fail-closed ``writer_unknown`` — and so is any commit whose writer
        lines the parse cannot return whole: a Writer-shaped line OUTSIDE the
        trailing trailer block (see :func:`stray_writer_line`), a
        writer-shaped junk key inside it, a Writer-shaped continuation, or a
        continuation folded onto a ``Writer`` value (see
        :func:`hidden_writer_line`).  Each of those names a writer no seat
        may cover, so a partial family list is never returned.
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
            trailers = self._leaf_trailers(repo, sha)
            return [] if trailers is None else [trailers]
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
        out: list[dict[str, str | list[str]]] = []
        for line in rev.stdout.splitlines():
            commit = line.strip().lower()
            if not valid_commit_ref(commit):
                continue
            trailers = self._leaf_trailers(repo, commit)
            if trailers is None:
                # One unknowable leaf poisons the whole writer set: never let
                # the other leaves' families stand for a writer we dropped.
                return []
            out.append(trailers)
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
        project = (item or {}).get("project")
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
