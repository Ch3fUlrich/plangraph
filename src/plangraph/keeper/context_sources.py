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

from .evidence_decisions import _decision_cell
from .evidence_git import valid_ref
from .evidence_types import valid_commit_ref

__all__ = ["plan_item_commits", "open_lanes", "decision_ids", "decision_rows",
           "decisions_naming", "superseded_map", "COMMIT_LIMIT"]

COMMIT_LIMIT = 5
_LANE_LIMIT = 5
_REF_COUNT = 5000
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
    prefix = f"({re.escape(proj)}:)?" if proj else ""
    out = _ok(git, repo, [
        "log", "--no-show-signature", "-E", "--grep",
        f"^Plan-Item:[[:space:]]*{prefix}{re.escape(bare)}[[:space:]]*$",
        "-n", str(COMMIT_LIMIT * 4),
        "--format=%H%x1f%cI%x1f%s%x1f%(trailers:key=Plan-Item,valueonly,separator=%x1e)%x1d",
        "--end-of-options", ref])
    if out is None:
        return None
    commits = []
    for record in out.split("\x1d"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 4:
            continue
        sha, date, subject, trailers = fields
        names = {value.strip() for value in trailers.split("\x1e") if value.strip()}
        if (key in names or bare in names) and valid_commit_ref(sha.strip()):
            commits.append({"sha": sha.strip(), "date": date, "subject": subject})
        if len(commits) >= COMMIT_LIMIT:
            break
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
    out = _ok(git, repo, ["for-each-ref", f"--count={_REF_COUNT}",
                          "--format=%(refname:short)", "refs/heads", "refs/remotes"])
    if out is None:
        return None
    slug = bare.lower()
    remotes = (_ok(git, repo, ["remote"]) or "").split()
    lanes, seen = [], set()
    for name in out.split():
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
_CACHE_LOCK = threading.Lock()
_CACHE = {}


def decision_rows(decisions):
    """``{"D-n": [row lines]}`` from a :class:`DecisionsAdapter` (empty if none).

    Parsed once per file version (path, mtime, size), so reads stay cheap.
    """
    if decisions is None:
        return {}
    path = getattr(decisions, "_path", None)
    try:
        stat = os.stat(path) if path else None
        stamp = (str(path), stat.st_mtime_ns, stat.st_size) if stat else None
    except OSError:
        stamp = None
    if stamp is not None:
        with _CACHE_LOCK:
            if _CACHE.get("stamp") == stamp:
                return _CACHE["rows"]
    try:
        rows = {did: list(lines) for did, lines in decisions._rows().items()}
    except Exception:
        return {}
    if stamp is not None:
        with _CACHE_LOCK:
            _CACHE.clear()
            _CACHE.update(stamp=stamp, rows=rows, superseded=None)
    return rows


def decisions_naming(rows, key):
    """Decision ids whose row (of :func:`decision_rows`) names *key*, by number.

    The full ``project:ID`` key always counts; the bare id counts when it is
    distinctive (contains ``-`` and is at least 6 chars), since rows often
    drop the project prefix.
    """
    bare = key.split(":", 1)[-1]
    patterns = [re.compile(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])")]
    if "-" in bare and len(bare) >= 6:
        patterns.append(re.compile(r"(?<![\w:-])" + re.escape(bare) + r"(?![\w-])"))
    hits = [did for did, lines in rows.items()
            if any(p.search(line) for line in lines for p in patterns)]
    return sorted(hits, key=_did_num)


def superseded_map(rows):
    """``{"D-old": "D-new"}``: decisions a later row retires.

    Row ``D-new`` says ``supersedes|corrects|withdraws|replaces D-old``, or
    row ``D-old``'s DECISION cell opens with ``WITHDRAWN|superseded by D-new``.
    """
    with _CACHE_LOCK:
        if _CACHE.get("rows") is rows and _CACHE.get("superseded") is not None:
            return _CACHE["superseded"]
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
    with _CACHE_LOCK:
        if _CACHE.get("rows") is rows:
            _CACHE["superseded"] = out
    return out
