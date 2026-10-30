"""Outside facts for an item's task context (TASK-CONTEXT-GUARD, D-3215).

Two read-only sources, both best effort (a failure means "not known", never a
crash, and an unknown never refuses a claim):

* git: commits on the project's target ref whose ``Plan-Item`` trailer names
  the key, the merge (PR) that brought the newest one in, and unmerged
  branches whose name carries the item id (open lanes / PR heads).
* decisions: the D-NNN rows of the configured decisions log that name the key,
  and which decisions a later row supersedes, corrects or withdraws.

The git side reuses :class:`GitAdapter`'s argument-list runner (bounded
timeout, no shell), ref validation and rate-limited fetch.  Keys are checked
against a strict character set before they reach a git argument.
"""
from __future__ import annotations

import os
import re
import threading
import time

from .evidence_decisions import _decision_cell
from .evidence_git import valid_ref
from .evidence_types import valid_commit_ref

__all__ = ["plan_item_commits", "open_lanes", "decision_ids", "decision_book",
           "decisions_naming", "superseded_map", "COMMIT_LIMIT"]

COMMIT_LIMIT = 5
_LANE_LIMIT = 5
_REF_COUNT = 5000
_GREP_WINDOW = COMMIT_LIMIT * 4
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?\Z")
_PR_RE = re.compile(r"#(\d+)")
_DID_RE = re.compile(r"(?<![\w-])[Dd]-0*(\d{1,6})(?!\d)")
# Row 40 "supersedes / corrects / withdraws / replaces D-12" retires D-12.
_SUPERSEDES_RE = re.compile(
    r"(?i)\b(?:supersedes|corrects|withdraws|replaces)\s+D-0*(\d+)\b")
# Row 12 whose DECISION cell opens with "WITHDRAWN by D-40" / "superseded by
# D-40" retires itself; the same phrase later in a cell may be about anything.
_OWN_RETIRED_RE = re.compile(
    r"(?i)^\W{0,3}(?:superseded|withdrawn|replaced|corrected)\s+by\s+D-0*(\d+)\b")


def decision_ids(*texts) -> list:
    """The ``D-NNN`` ids cited in *texts*, canonical and sorted by number."""
    found = set()
    for text in texts:
        if isinstance(text, str):
            found.update(int(n) for n in _DID_RE.findall(text))
    return [f"D-{n}" for n in sorted(found)]


def _did_num(did):
    try:
        return int(str(did).split("-", 1)[1])
    except (IndexError, ValueError):
        return -1


# ------------------------------------------------------------------ git
def _repo_ref(git, project):
    if git is None:
        return None, None
    try:
        repo = git.repo_path(project)
        ref = git.repo_ref(project) if repo else None
    except Exception:
        return None, None
    if not repo or not ref or not valid_ref(ref):
        return None, None
    return repo, ref


def _git(git, repo, args):
    """``(returncode, stdout)``; ``(None, None)`` when git could not run."""
    try:
        proc = git._run(repo, args)
    except Exception:
        return None, None
    out = proc.stdout if isinstance(proc.stdout, str) else None
    return proc.returncode, out


def _ok(git, repo, args):
    code, out = _git(git, repo, args)
    return out if code == 0 else None


def _ere(text):
    """*text* as a POSIX ERE literal (``re.escape`` would also escape ``-``)."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text)


def _split(key):
    """``(project, bare id)`` of a well-formed key, else ``(None, None)``."""
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        return None, None
    project, _, bare = key.rpartition(":")
    return (project or None), bare


def plan_item_commits(git, project, key, *, fetch=True):
    """Commits on the default ref with a ``Plan-Item: <key>`` trailer, newest first.

    Returns ``None`` when unknown (no repo, malformed key, git failure), else a
    list of ``{sha, date, subject}`` (at most :data:`COMMIT_LIMIT`); the newest
    carries ``merge`` = ``{sha, subject, pr}`` when a merge on the ref's
    first-parent chain brought it in.  git itself selects only messages with
    a ``Plan-Item:`` line equal to the full key or the bare id (anchored
    regex), and the parsed trailer is compared exactly again: ``X`` never
    matches ``X-FU`` and a later mention elsewhere never counts.
    """
    repo, ref = _repo_ref(git, project)
    proj, bare = _split(key)
    if repo is None or not bare:
        return None
    if fetch:
        try:
            git._maybe_fetch(repo)
        except Exception:
            pass
    prefix = f"({_ere(proj)}:)?" if proj else ""
    out = _ok(git, repo, [
        "log", "--no-show-signature", "-E", "--grep",
        f"^Plan-Item:[[:space:]]*{prefix}{_ere(bare)}[[:space:]]*$",
        "-n", str(_GREP_WINDOW),
        "--format=%H%x1f%cI%x1f%s%x1f%(trailers:key=Plan-Item,valueonly,separator=%x1e)%x1d",
        "--end-of-options", ref])
    if out is None:
        return None
    commits, seen = [], 0
    for record in out.split("\x1d"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 4:
            continue
        seen += 1
        sha, date, subject, trailers = fields
        names = {value.strip() for value in trailers.split("\x1e") if value.strip()}
        if (key in names or bare in names) and valid_commit_ref(sha.strip()):
            commits.append({"sha": sha.strip(), "date": date, "subject": subject})
        if len(commits) >= COMMIT_LIMIT:
            break
    if not commits and seen >= _GREP_WINDOW:
        return None              # window full of near misses: not proven absent
    if commits:
        merge = _merge_of(git, repo, ref, commits[0]["sha"])
        if merge:
            commits[0]["merge"] = merge
    return commits


def _merge_of(git, repo, ref, sha):
    """The first-parent merge on *ref* that introduced *sha*, or ``None``."""
    out = _ok(git, repo, ["rev-list", "--ancestry-path", "--first-parent",
                          "--merges", "--end-of-options", f"{sha}..{ref}"])
    found = (out or "").split()
    if not found or not valid_commit_ref(found[-1]):
        return None
    subject = (_ok(git, repo, ["show", "-s", "--no-show-signature", "--format=%s",
                               "--end-of-options", found[-1]]) or "").strip()
    match = _PR_RE.search(subject)
    return {"sha": found[-1], "subject": subject,
            "pr": int(match.group(1)) if match else None}


_REFS_LOCK = threading.Lock()
_REFS = {}
_REFS_TTL = 10.0


def _refs(git, repo):
    """``(branch names, remote names)`` of *repo*, reused for ``_REFS_TTL`` s.

    One listing serves every row of a ``ready``.  ``None`` = unknown: git
    failed, or the listing hit ``_REF_COUNT`` and may be incomplete.
    """
    now = time.monotonic()
    with _REFS_LOCK:
        hit = _REFS.get(repo)
        if hit is not None and now - hit[0] < _REFS_TTL:
            return hit[1]
    out = _ok(git, repo, ["for-each-ref", f"--count={_REF_COUNT}",
                          "--format=%(refname:short)", "refs/heads", "refs/remotes"])
    remotes = _ok(git, repo, ["remote"])
    if out is None or remotes is None:
        return None
    names = out.split()
    if len(names) >= _REF_COUNT:
        return None
    snapshot = (tuple(names), frozenset(remotes.split()))
    with _REFS_LOCK:
        _REFS[repo] = (now, snapshot)
    return snapshot


def open_lanes(git, project, key):
    """Branches whose name carries the item id and that are not on the ref.

    ``None`` when unknown (no repo, git failure).  Local and remote copies of
    one branch count once (by name without the remote).  A PR head is a
    branch, so an open PR shows by its branch name (``lane/<id lower>``).
    """
    repo, ref = _repo_ref(git, project)
    _, bare = _split(key)
    if repo is None or not bare:
        return None
    snapshot = _refs(git, repo)
    if snapshot is None:
        return None
    names, remotes = snapshot
    slug = bare.lower()
    lanes, seen = [], set()
    for name in names:
        if slug not in name.lower() or name == ref or not valid_ref(name):
            continue
        head, _, rest = name.partition("/")
        short = rest if head in remotes and rest else name
        if short in seen:
            continue
        code, _ = _git(git, repo, ["merge-base", "--is-ancestor",
                                   "--end-of-options", name, ref])
        if code == 0:            # already on the ref: finished, not open
            continue
        if code != 1:            # git error: the whole answer is unknown
            return None
        seen.add(short)
        lanes.append(name)
        if len(lanes) >= _LANE_LIMIT:
            break
    return lanes


# ------------------------------------------------------------ decisions
_BOOK_LOCK = threading.Lock()
_BOOKS = {}
_BOOK_SLOTS = 3
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def decision_book(decisions):
    """``{"rows", "superseded", "index"}`` for a :class:`DecisionsAdapter`.

    ``rows`` = ``{"D-n": [row lines]}``, ``superseded`` = :func:`superseded_map`
    and ``index`` = ``{token: {D-ids}}`` over every key-like token, so naming
    lookups cost one dict probe.  Built once per file version (path, inode,
    mtime, size; up to three files kept); an unreadable source is an empty book.
    """
    path = getattr(decisions, "_path", None) if decisions is not None else None
    if path is None:
        return _build_book({})
    try:
        stat = os.stat(path)
    except OSError:
        return _build_book({})
    stamp = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    with _BOOK_LOCK:
        book = _BOOKS.get(stamp)
    if book is not None:
        return book
    try:
        rows = {did: tuple(lines) for did, lines in decisions._rows().items()}
    except Exception:
        rows = {}
    book = _build_book(rows)
    with _BOOK_LOCK:
        for old in [k for k in _BOOKS if k[0] == stamp[0]]:
            del _BOOKS[old]
        while len(_BOOKS) >= _BOOK_SLOTS:
            del _BOOKS[next(iter(_BOOKS))]
        _BOOKS[stamp] = book
    return book


def _build_book(rows):
    index = {}
    for did, lines in rows.items():
        for line in lines:
            for token in _TOKEN_RE.findall(line):
                token = token.strip(".:")
                if token:
                    index.setdefault(token, set()).add(did)
    return {"rows": rows, "superseded": superseded_map(rows), "index": index}


def decisions_naming(book, key):
    """Decision ids whose row names *key*, sorted by number.

    The full ``project:ID`` key always counts; the bare id counts when it is
    distinctive (contains ``-`` and is at least 6 chars), since rows often
    drop the project prefix.  A longer id (``X-FU``) is a different token.
    """
    bare = key.split(":", 1)[-1]
    hits = set(book["index"].get(key, ()))
    if "-" in bare and len(bare) >= 6:
        hits |= book["index"].get(bare, set())
    return sorted(hits, key=_did_num)


def superseded_map(rows):
    """``{"D-old": "D-new"}``: decisions a later row retires.

    Row ``D-new`` says ``supersedes|corrects|withdraws|replaces D-old``, or
    row ``D-old``'s DECISION cell opens with ``WITHDRAWN|superseded by D-new``.
    Only the DECISION cell (3rd column) is read.
    """
    out = {}
    for did, lines in rows.items():
        own = _did_num(did)
        for line in lines:
            cell = _decision_cell(line)
            match = _OWN_RETIRED_RE.match(cell)
            if match and int(match.group(1)) > own:
                out.setdefault(did, f"D-{int(match.group(1))}")
            for found in _SUPERSEDES_RE.finditer(cell):
                old = f"D-{int(found.group(1))}"
                newest = out.get(old)
                if _did_num(old) < own and (newest is None or _did_num(newest) < own):
                    out[old] = did
    return out
