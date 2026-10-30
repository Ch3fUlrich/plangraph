"""Outside facts for an item's task context (TASK-CONTEXT-GUARD, D-3215).

Two read-only sources, both best effort (a failure means "not known", never a
crash, and an unknown never refuses a claim):

* git: commits on the project's target ref whose ``Plan-Item`` trailer names
  the key, the merge (PR) that brought the newest one in, and unmerged
  branches whose name carries the item id (open lanes / PR heads).
* decisions: the D-NNN rows of the configured decisions log that name the key,
  and which decisions a later row supersedes, corrects or withdraws.

The git side reuses :class:`GitAdapter`'s argument-list runner, ref validation
and rate-limited fetch; nothing is passed through a shell.
"""
from __future__ import annotations

import re

from .evidence_git import valid_ref
from .evidence_types import valid_commit_ref

__all__ = ["plan_item_commits", "open_lanes", "decision_ids", "decision_rows",
           "decisions_naming", "superseded_map", "COMMIT_LIMIT"]

COMMIT_LIMIT = 5
_LANE_LIMIT = 5
_PR_RE = re.compile(r"#(\d+)")
_DID_RE = re.compile(r"(?<![\w-])[Dd]-0*(\d{1,6})(?!\d)")
# "D-12 superseded by D-40", "D-12 WITHDRAWN by D-40" inside row 12, or
# "supersedes D-12" / "corrects D-12" / "withdraws D-12" in row 40.
_SUPERSEDED_BY_RE = re.compile(
    r"(?i)(?:superseded|withdrawn|replaced|corrected)\s+by\s+D-0*(\d+)")
_SUPERSEDES_RE = re.compile(
    r"(?i)\b(?:supersedes|corrects|withdraws|replaces)\s+D-0*(\d+)")
_OBSOLETE_RE = re.compile(r"(?i)D-0*(\d+)\b[^|;.]{0,30}?\b(?:superseded|withdrawn)\b")


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
    try:
        proc = git._run(repo, args)
    except Exception:
        return None
    if proc.returncode != 0 or not isinstance(proc.stdout, str):
        return None
    return proc.stdout


def _bare(key):
    return key.split(":", 1)[-1]


def plan_item_commits(git, project, key, *, fetch=True):
    """Commits on the default ref with a ``Plan-Item: <key>`` trailer, newest first.

    Returns ``None`` when the project has no repo or git fails (unknown), else
    a list of ``{sha, date, subject}`` (at most :data:`COMMIT_LIMIT`); the newest
    carries ``merge`` = ``{sha, subject, pr}`` when a merge on the ref's
    first-parent chain brought it in.  The trailer must equal the full key or
    the bare id — ``X`` never matches ``X-FU``.
    """
    repo, ref = _repo_ref(git, project)
    if repo is None:
        return None
    if fetch:
        try:
            git._maybe_fetch(repo)
        except Exception:
            pass
    bare = _bare(key)
    out = _git(git, repo, [
        "log", "--no-show-signature", "-F", "--grep", bare, "-n", "200",
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
    out = _git(git, repo, ["rev-list", "--ancestry-path", "--first-parent",
                           "--merges", "--reverse", "--end-of-options",
                           f"{sha}..{ref}"])
    first = (out or "").split()
    if not first or not valid_commit_ref(first[0]):
        return None
    subject = (_git(git, repo, ["show", "-s", "--no-show-signature", "--format=%s",
                                "--end-of-options", first[0]]) or "").strip()
    match = _PR_RE.search(subject)
    return {"sha": first[0], "subject": subject,
            "pr": int(match.group(1)) if match else None}


def open_lanes(git, project, key):
    """Branches (local + remote) whose name carries the item id, not yet on the ref.

    ``None`` when unknown.  A PR head is a branch, so an open PR for the item
    shows here by its branch name (``lane/<id lower>`` by convention).
    """
    repo, ref = _repo_ref(git, project)
    if repo is None:
        return None
    out = _git(git, repo, ["for-each-ref", "--format=%(refname:short)",
                           "refs/heads", "refs/remotes"])
    if out is None:
        return None
    slug = _bare(key).lower()
    lanes = []
    for name in out.split():
        if slug not in name.lower() or name == ref or not valid_ref(name):
            continue
        # merged into the ref = finished work, not an open lane
        merged = _git(git, repo, ["merge-base", "--is-ancestor",
                                  "--end-of-options", name, ref])
        if merged is None:
            lanes.append(name)
        if len(lanes) >= _LANE_LIMIT:
            break
    return lanes


# ------------------------------------------------------------ decisions
def decision_rows(decisions):
    """``{"D-n": "row text"}`` from a :class:`DecisionsAdapter` (empty if none)."""
    if decisions is None:
        return {}
    try:
        rows = decisions._rows()
    except Exception:
        return {}
    return {did: " ".join(lines) for did, lines in rows.items()}


def decisions_naming(rows, key):
    """Decision ids whose row (of :func:`decision_rows`) names *key*, by number.

    The full ``project:ID`` key always counts; the bare id counts when it is
    distinctive (contains ``-`` and is at least 6 chars), since rows often
    drop the project prefix.
    """
    bare = _bare(key)
    patterns = [re.compile(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])")]
    if "-" in bare and len(bare) >= 6:
        patterns.append(re.compile(r"(?<![\w:-])" + re.escape(bare) + r"(?![\w-])"))
    hits = [did for did, text in rows.items()
            if any(p.search(text) for p in patterns)]
    return sorted(hits, key=_did_num)


def superseded_map(rows):
    """``{"D-old": "D-new"}``: decisions a later row supersedes/corrects/withdraws."""
    out = {}
    for did, text in rows.items():
        own = _did_num(did)
        for match in _SUPERSEDED_BY_RE.finditer(text):
            newer = int(match.group(1))
            if newer > own:
                out.setdefault(did, f"D-{newer}")
        for regex in (_SUPERSEDES_RE, _OBSOLETE_RE):
            for match in regex.finditer(text):
                old = int(match.group(1))
                if old < own:
                    prior = out.get(f"D-{old}")
                    if prior is None or _did_num(prior) < own:
                        out[f"D-{old}"] = did
    return out
