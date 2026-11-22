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

import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .errors import Refused
from .evidence_decisions import DecisionsAdapter, _decision_cell, _did_num, superseded_map
from .evidence_git import REVERT_GREP, delivery_revert, is_first_parent_merge, plan_items, valid_ref
from .evidence_types import valid_commit_ref

__all__ = ["plan_item_commits", "open_lanes", "pr_merge_of", "decision_ids", "decision_book", "decision_texts",
           "decisions_naming", "superseded_map", "COMMIT_LIMIT"]

COMMIT_LIMIT = 5
_LANE_LIMIT = 5
_REF_COUNT = 5000
_GREP_WINDOW = COMMIT_LIMIT * 4
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?\Z")
_PR_RE = re.compile(r"#(\d+)")
_PR_SUFFIX_RE = re.compile(r"\(#([1-9][0-9]*)\)\Z")
MERGE_REPO_LINE = re.compile(
    r"[ \t]*Merge-Repo:[ \t]*([A-Za-z0-9_.-]+)"
    r"(?:[ \t]+([a-fA-F0-9]{40}|#[1-9][0-9]*))?[ \t]*")
_DID_RE = re.compile(r"(?<![\w-])[Dd]-0*(\d{1,6})(?!\d)")


def decision_ids(*texts) -> list:
    """The ``D-NNN`` ids cited in *texts*, canonical and sorted by number."""
    found = set()
    for text in texts:
        if isinstance(text, str):
            found.update(int(n) for n in _DID_RE.findall(text))
    return [f"D-{n}" for n in sorted(found)]


def subject_pr(subject):
    """Read the trailing canonical PR number, falling back to the first reference."""
    text = subject.strip()
    match = _PR_SUFFIX_RE.search(text) or _PR_RE.search(text)
    return int(match[1]) if match else None


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


def _cancelled_delivery(git, repo, candidates, reverts):
    cancelled = set()
    for sha, (target, names) in reverts.items():  # Descendants precede their ancestors.
        if sha in cancelled:
            continue
        parents = _ok(git, repo, ["rev-list", "--parents", "-n1", "--end-of-options", target]) if target else None
        if parents:
            commit, *previous = parents.split()
            if not previous:
                cancelled.add(commit)
                continue
            removed = _ok(git, repo, ["rev-list", "--end-of-options", commit, f"^{previous[0]}"])
            if removed is not None:
                cancelled.update(removed.split())
                continue
        for commit, keys in candidates.items():
            if names.intersection(keys) and _git(
                    git, repo, ["merge-base", "--is-ancestor", "--end-of-options", commit, sha])[0] == 0:
                cancelled.add(commit)
    return cancelled


def work_index(git, project, *, fresh=False, max_age=None, cached_only=False):
    """One cached origin-refreshed snapshot; no item-dependent subprocesses.

    Read all commit trailers and topology together, including hosted open PR
    heads. Unknown inputs remain unknown, as in the claim guard.
    """
    repo, ref = _repo_ref(git, project)
    if repo is None:
        return None
    cache = getattr(git, "_context_work_index", {})
    cache_key = (repo, ref)
    hit = cache.get(cache_key)
    if cached_only:  # a caller holding the keeper lock: the warm answer or nothing, never git or the forge
        if hit and time.monotonic() - hit[0] < (_REFS_TTL if max_age is None else max_age):
            return hit[1]
        raise Refused("evidence_unverified", f"merge_index_not_warm: {project}")
    if not fresh and hit and time.monotonic() - hit[0] < (_REFS_TTL if max_age is None else max_age):
        return hit[1]
    problems = []
    verified_at = (hit[1].get("verified_at") if hit else None)
    prs = None
    origin = _ok(git, repo, ["remote", "get-url", "origin"]) or ""
    # Always observe this fetch's result, including for remote-tracking refs.
    if origin:
        if _ok(git, repo, ["fetch", "--no-tags", "origin"]) is None:
            problems.append("origin fetch failed or timed out")
        else:
            verified_at = time.time()
        branch = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
        remote_ref = "refs/remotes/origin/" + branch.removeprefix("origin/")
        if _ok(git, repo, ["rev-parse", "--verify", "--end-of-options", remote_ref]):
            ref = remote_ref
        else:
            problems.append("origin target ref unavailable")
    origin_stale = bool(problems)
    prs_expected = bool(_github_repo(origin.strip()))
    if prs_expected:
        try:
            proc = subprocess.run(
                ["gh", "pr", "list", "--state", "open", "--limit", "10000",
                 "--json", "number,headRefOid,headRefName"], cwd=repo,
                capture_output=True, text=True, timeout=30, check=False)
            value = json.loads(proc.stdout) if proc.returncode == 0 else None
            if isinstance(value, list) and len(value) < 10000 and all(
                    isinstance(p, dict) and type(p.get("number")) is int
                    and p["number"] > 0 and valid_commit_ref(p.get("headRefOid"))
                    for p in value):
                prs = value
                if prs:
                    if _ok(git, repo, ["fetch", "--no-tags", "origin", *[
                            f"+refs/pull/{p['number']}/head:refs/keeper/pull/{p['number']}/head"
                            for p in prs]]) is None:
                        prs = None
                        problems.append("PR head fetch failed or timed out")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if prs is None and not problems:
            problems.append("open PR inventory unavailable")
    refs_out = _ok(git, repo, ["for-each-ref", "--format=%(refname) %(objectname)"])
    target = _ok(git, repo, ["rev-parse", "--verify", "--end-of-options", ref])
    out = _ok(git, repo, ["log", "--all", "--date-order", "--no-show-signature",
                         "--format=%H%x1f%P%x1f%cI%x1f%s%x1f%B%x1d"])
    if refs_out is None or target is None or out is None:
        if not origin:
            return None
        origin_stale = True
        problems.append("repository index unavailable")
    records, reverts = {}, {}
    for record in (out or "").split("\x1d"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) == 5 and valid_commit_ref(fields[0]):
            sha, parents, date, subject, trailers = fields
            if (revert := delivery_revert(trailers)) is not None:
                reverts[sha] = revert
            records[sha] = (parents.split(), {"sha": sha, "date": date,
                            "subject": subject}, [] if sha in reverts else plan_items(trailers))

    def ancestors(head):
        seen, todo = set(), [head]
        while todo:
            sha = todo.pop()
            if sha in seen:
                continue
            seen.add(sha)
            todo.extend(records.get(sha, ([],))[0])
        return seen

    if prs is not None and any(p["headRefOid"] not in records for p in prs):
        prs = None
        problems.append("PR head commit unavailable")
    merged = ancestors((target or "").strip())
    # Annotate introducing merges once from the same refreshed target topology.
    chain, cursor = [], (target or "").strip()
    while cursor in records:
        chain.append(cursor)
        parents = records[cursor][0]
        cursor = parents[0] if parents else None
    seen = set()
    for landing in reversed(chain):
        parents, commit, _ = records[landing]
        todo, introduced = [landing], []
        while todo:
            sha = todo.pop()
            if sha in seen:
                continue
            seen.add(sha)
            introduced.append(sha)
            todo.extend(records.get(sha, ([],))[0])
        if len(parents) > 1:
            pr = subject_pr(commit["subject"])
            merge = {"sha": landing, "subject": commit["subject"], "pr": pr}
            own_pr = len(parents) == 2 and pr
            for sha in introduced:
                if sha in records and (sha != landing or own_pr):
                    records[sha][1]["merge"] = merge
    commits, pulls = {}, {}
    cancelled = _cancelled_delivery(git, repo, {s: records[s][2] for s in merged if s in records and records[s][2]},
                                    {s: r for s, r in reverts.items() if s in merged})
    for sha, (_, commit, names) in records.items():
        if sha in merged and sha not in cancelled:
            for name in names:
                commits.setdefault(name.strip(), []).append(commit)
    for pr in prs or []:
        for sha in ancestors(pr["headRefOid"]) - merged:
            for name in records.get(sha, ([], {}, []))[2]:
                pulls.setdefault(name.strip(), []).append(pr["number"])
    branches = []
    for line in (refs_out or "").splitlines():
        name, sha = line.split(" ", 1)
        if name.startswith(("refs/heads/", "refs/remotes/")) and sha not in merged:
            branches.append(name.removeprefix("refs/heads/").removeprefix("refs/remotes/"))
    prs_unverified = prs_expected and (prs is None or origin_stale)
    previous_prs = hit[1].get("last_verified_prs") if hit and prs_expected else None
    verified_prs = pulls if prs is not None and not prs_unverified else None
    result = {"commits": commits, "prs": verified_prs,
              "last_verified_prs": verified_prs if verified_prs is not None else previous_prs,
              "prs_unverified": prs_unverified,
              "branches": branches, "order": {sha: i for i, sha in enumerate(records)},
              "stale": bool(problems), "origin_stale": origin_stale,
              "reason": "; ".join(problems),
              "verified_at": verified_at}
    cache[cache_key] = (time.monotonic(), result)
    git._context_work_index = cache
    return result


_REFRESHING = set()
_REFRESH_LOCK = threading.Lock()


def work_index_cached(git, project):
    """The ready read's index: never rebuilt inside a request once one exists.

    ``work_index`` fetches origin, lists open PRs (``gh``, up to 30 s) and walks
    ``git log --all``: 24 s held under the keeper lock on the live plan, and the
    10 s cache expired between any two reads, so every ``ready`` paid it.  A read
    now serves the last built index whatever its age and starts ONE background
    rebuild when it is older than ``_REFS_TTL``; only the very first read after a
    start (nothing to serve) builds in the request.  ``verified_at`` still
    carries the index's age, so a stale answer says so.
    """
    repo, ref = _repo_ref(git, project)
    if repo is None:
        return work_index(git, project)         # no repo: answers at once
    key = (repo, ref)
    hit = getattr(git, "_context_work_index", {}).get(key)
    if hit is None:
        return work_index(git, project)
    age = time.monotonic() - hit[0]
    if age >= _REFS_TTL:
        # The served index says it is expired, so the reader can warn.
        served = dict(hit[1], refreshing=True, built_age_s=int(age))
        with _REFRESH_LOCK:
            if key in _REFRESHING:
                return served
            _REFRESHING.add(key)

        def refresh():
            try:
                work_index(git, project, fresh=True)
            except Exception:           # the old index keeps serving
                pass
            finally:
                with _REFRESH_LOCK:
                    _REFRESHING.discard(key)
        threading.Thread(target=refresh, name="work-index-refresh",
                         daemon=True).start()
        return served
    return hit[1]


def indexed_open_pulls(indexes, key, project, *, cached=False):
    """Match exact trailers across repositories; bare IDs belong to their own project."""
    _, bare = _split(key)
    pulls = set()
    for name, index in indexes.items():
        prs = (index or {}).get("last_verified_prs" if cached else "prs") or {}
        pulls.update(prs.get(key, ()))
        if name == project:
            pulls.update(prs.get(bare, ()))
    return sorted(pulls)


def indexed_work(index, key):
    """Resolve full/bare trailers and lane names without I/O."""
    if index is None or index.get("origin_stale"):
        return {"commits": None, "lanes": None, "prs": None}
    _, bare = _split(key)
    names = (key, bare) if key != bare else (key,)
    commits = {c["sha"]: c for name in names
               for c in index["commits"].get(name, [])}
    prs = index["prs"]
    ordered = sorted(commits.values(), key=lambda c: index.get("order", {}).get(c["sha"], 0))
    return {"commits": ordered[:COMMIT_LIMIT],
            "lanes": [b for b in index["branches"] if bare.lower() in b.lower()],
            "prs": sorted({p for name in names for p in prs.get(name, [])})
            if prs is not None else None}


def plan_item_commits(git, project, key, *, fetch=True, limit=COMMIT_LIMIT, head=None):
    """Commits with a ``Plan-Item: <key>`` trailer, newest first, optionally at a pinned head.

    Returns None when unknown. A finite limit bounds the tagged candidates;
    ``limit=None`` returns the whole matching history and annotates every merge.
    The parsed trailer must match exactly after the git message prefilter.
    """
    repo, ref = _repo_ref(git, project)
    proj, bare = _split(key)
    if repo is None or not bare or (head is not None and not valid_commit_ref(head)):
        return None
    if fetch and head is None:
        try:
            git._maybe_fetch(repo)
        except Exception:
            pass
    ref = head or ref
    prefix = f"({_ere(proj)}:)?" if proj else ""
    pattern = f"^Plan-Item:[ \t]*{prefix}{_ere(bare)}[ \t]*$"
    out = _ok(git, repo, [
        "log", "--date-order", "--no-show-signature", "-E", "--grep", pattern,
        "--regexp-ignore-case", "--grep", REVERT_GREP,
        "--format=%H%x1f%cI%x1f%s%x1f%B%x1d", "--end-of-options", ref])
    if out is None:
        return None
    hits, candidates, reverts, seen = [], {}, {}, 0
    for record in out.split("\x1d"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 4 or not valid_commit_ref(fields[0]):
            continue
        sha, date, subject, message = fields
        if (revert := delivery_revert(message)) is not None:
            reverts[sha] = revert
        elif re.search(pattern, message, re.M | re.I):
            seen += 1
            if (limit is None or seen <= _GREP_WINDOW) and (names := {key, bare}.intersection(plan_items(message))):
                candidates[sha] = names
                hits.append({"sha": sha, "date": date, "subject": subject})
    cancelled = _cancelled_delivery(git, repo, candidates, reverts) if candidates else ()
    commits = [c for c in hits if c["sha"] not in cancelled][:limit]
    if limit is not None and not commits and seen >= _GREP_WINDOW:
        return None
    for commit in (commits[:1] if limit is not None else commits):
        merge = _merge_of(git, repo, ref, commit["sha"])
        if merge:
            commit["merge"] = merge
    return commits


def _merge_of(git, repo, ref, sha):
    """The first-parent merge on *ref* that introduced *sha*, or ``None``."""
    own_merge = is_first_parent_merge(git, repo, sha, ref)
    if own_merge is None:
        return None
    if own_merge:
        subject = (_ok(git, repo, ["show", "-s", "--no-show-signature", "--format=%s",
                                   "--end-of-options", sha]) or "").strip()
        return {"sha": sha, "subject": subject, "pr": subject_pr(subject)}
    # Combining --ancestry-path with --first-parent prunes paths through
    # second parents (nested batches and PR branches refreshed from main).
    # Walk ancestry separately, then intersect with the ref's landing merges.
    ancestry = _ok(git, repo, ["rev-list", "--ancestry-path", "--merges",
                              "--end-of-options", f"{sha}..{ref}"])
    if not ancestry:
        return None
    # The whole chain between *sha* and *ref* is listed: containing merges form
    # a prefix of it, so its last member is the introducing merge however far
    # behind *ref* the commit sits. One listing costs the same as the ancestry walk.
    out = _ok(git, repo, ["rev-list", "--first-parent", "--merges",
                          "--end-of-options", f"{sha}..{ref}"])
    if out is None:
        return None
    ancestors = set(ancestry.split())
    found = [commit for commit in out.split() if commit in ancestors]
    if not found or not valid_commit_ref(found[-1]):
        return None
    subject = (_ok(git, repo, ["show", "-s", "--no-show-signature", "--format=%s",
                               "--end-of-options", found[-1]]) or "").strip()
    return {"sha": found[-1], "subject": subject, "pr": subject_pr(subject)}


def _head_introduced_by_merge(git, repo, parents, head):
    """True when *head* entered via *parents[1]* of a two-parent merge, not *parents[0]*."""
    if len(parents) != 2 or not all(valid_commit_ref(p) for p in parents) or not valid_commit_ref(head):
        return False
    if parents[1] == head:
        return True
    p0_code, _ = _git(git, repo, ["merge-base", "--is-ancestor", "--end-of-options", head, parents[0]])
    if p0_code != 1:
        return False
    p1_code, _ = _git(git, repo, ["merge-base", "--is-ancestor", "--end-of-options", head, parents[1]])
    return p1_code == 0


def pr_merge_of(git, project, head, *, fallback_pr=None, default_head=None):
    """``{pr, merge, head}`` when *head* was introduced on the second parent of the
    first-parent merge on the default ref, else ``None`` (also when git cannot answer).

    When the merge subject carries no ``#N``, *fallback_pr* (from the GO row) supplies
    the PR number and *head* may be any commit introduced on the merge's second parent;
    otherwise the subject's ``#N`` is authoritative and *head* must equal the second parent.
    """
    repo, ref = _repo_ref(git, project)
    if (repo is None or not valid_commit_ref(head)
            or (default_head is not None and not valid_commit_ref(default_head))):
        return None
    merge = _merge_of(git, repo, default_head or ref, head)
    if not merge:
        return None
    parents = (_ok(git, repo, ["show", "-s", "--format=%P", "--end-of-options",
                               merge["sha"]]) or "").split()
    if len(parents) != 2 or not all(valid_commit_ref(p) for p in parents):
        return None
    if merge["pr"] is not None:
        if parents[1] != head:
            return None
        return {"pr": merge["pr"], "merge": merge["sha"], "head": head}
    if type(fallback_pr) is not int or fallback_pr <= 0:
        return None
    if not _head_introduced_by_merge(git, repo, parents, head):
        return None
    return {"pr": fallback_pr, "merge": merge["sha"], "head": head}


def named_merge(git, project, revision, *, head=None):
    """Verify a named merge on the default ref. Refuse a PR known to be unmerged.

    Returns ``head`` (the PR head), ``pr`` and ``landed``: the commit that is on the default ref,
    which the deploy scan reads (a squash merge leaves the PR head off the ref).
    """
    repo, ref = _repo_ref(git, project)
    if repo is None or (head is not None and not valid_commit_ref(head)):
        return None
    ref = head or ref
    pr = None
    sha = revision.lower()
    if revision.startswith("#"):
        pr = int(revision[1:])
        origin = _ok(git, repo, ["remote", "get-url", "origin"])
        hosted = _github_repo((origin or "").strip())
        if not hosted:
            return None
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(pr), "--repo", hosted,
                 "--json", "state,mergeCommit,headRefOid"], cwd=repo, capture_output=True,
                text=True, timeout=20, check=True)
            info = json.loads(result.stdout)
            if info["state"] in ("OPEN", "CLOSED"):
                raise Refused("evidence_unverified",
                              f"merge_repo_pr_not_merged: {project} {revision} ({info['state']})")
            if info["state"] != "MERGED":
                return None
            sha = info["mergeCommit"]["oid"]
            pr_head = info["headRefOid"]
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return None
        if not valid_commit_ref(pr_head):
            return None
    if not valid_commit_ref(sha) or _git(
            git, repo, ["merge-base", "--is-ancestor", "--end-of-options", sha, ref])[0] != 0:
        return None
    if pr is not None:
        return {"head": pr_head, "pr": pr, "landed": sha}
    introduced = _merge_of(git, repo, ref, sha)
    landed = sha
    if introduced:
        on_first_parent = _git(git, repo, ["merge-base", "--is-ancestor",
                                          "--end-of-options", sha, f"{introduced['sha']}^1"])[0]
        if on_first_parent not in (0, 1):
            return None
        if on_first_parent == 1:
            landed = introduced["sha"]
    shown = _ok(git, repo, ["show", "-s", "--format=%P%x1f%s", "--end-of-options", landed])
    if shown is None:
        return None
    parent_text, _, subject = shown.partition("\x1f")
    parents = parent_text.split()
    return {"head": parents[1] if len(parents) == 2 else sha, "pr": subject_pr(subject),
            "landed": landed}


_REFS_LOCK = threading.Lock()
_REFS = {}
_REFS_TTL = 10.0


def _refs(git, repo, fresh=False):
    """``(branch names, remote names)`` of *repo*, reused for ``_REFS_TTL`` s.

    One listing serves every row of a ``ready``.  ``None`` = unknown: git
    failed, or the listing hit ``_REF_COUNT`` and may be incomplete.
    """
    now = time.monotonic()
    with _REFS_LOCK:
        hit = _REFS.get(repo)
        if not fresh and hit is not None and now - hit[0] < _REFS_TTL:
            return hit[1]
    out = _ok(git, repo, ["for-each-ref", f"--count={_REF_COUNT}",
                          "--format=%(refname)", "refs/heads", "refs/remotes"])
    remotes = _ok(git, repo, ["remote"])
    if out is None or remotes is None:
        return None
    names = out.split()
    if len(names) >= _REF_COUNT:
        return None
    snapshot = (tuple(names), frozenset(remotes.split()))
    with _REFS_LOCK:
        _REFS[repo] = (time.monotonic(), snapshot)
    return snapshot


def _github_repo(origin):
    ssh = "git" + "@" + "github.com:"
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return None
    path = origin[len(ssh):] if origin.startswith(ssh) else (
        parsed.path.strip("/") if parsed.hostname == "github.com" else "")
    path = path.removesuffix(".git")
    return path if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) else None


def _closed_lane_pr(git, repo, hosted, branch, head):
    """Return the verified closed PR number, zero for open work, None on error."""
    fields = "number,state,headRefOid,isCrossRepository"
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", hosted, "--head", branch, "--state", "all",
             "--limit", "100", "--json", fields], cwd=repo, capture_output=True,
            text=True, timeout=20, check=False)
        prs = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not isinstance(prs, list) or len(prs) >= 100 or any(
            not isinstance(pr, dict) or type(pr.get("number")) is not int
            or pr["number"] <= 0 or pr.get("state") not in ("OPEN", "CLOSED", "MERGED")
            or not valid_commit_ref(pr.get("headRefOid"))
            or type(pr.get("isCrossRepository")) is not bool for pr in prs):
        return None
    own = [pr for pr in prs if not pr["isCrossRepository"]]
    if any(pr["state"] == "OPEN" for pr in own):
        return 0
    for pr in own:
        if pr["state"] != "CLOSED" or pr["headRefOid"] != head:
            continue
        ref = f"refs/pull/{pr['number']}/head"
        branch_ref = "refs/heads/" + branch
        live = _ok(git, repo, ["ls-remote", "origin", ref, branch_ref])
        if live is None:
            return None
        refs = dict(reversed(line.split()) for line in live.splitlines() if len(line.split()) == 2)
        if refs.get(ref) == head and refs.get(branch_ref, head) == head:
            return pr["number"]
    return 0


def open_lanes(git, project, key, fresh=False, *, closed=None, cache=None, frozen=False, head=None):
    """Branches whose name carries the item id and that are not on the ref.

    ``None`` when unknown (no repo, git failure).  Local and remote copies of
    one branch count once (by name without the remote).  A PR head is a
    branch, so an open PR shows by its branch name (``lane/<id lower>``).
    """
    repo, ref = _repo_ref(git, project)
    _, bare = _split(key)
    if repo is None or not bare or (head is not None and not valid_commit_ref(head)):
        return None
    snapshot = _refs(git, repo, fresh)
    if snapshot is None:
        return None
    names, remotes = snapshot
    target = head or ref
    slug = bare.lower()
    lanes, closed_prs = {}, set()
    verified = {} if cache is None else cache  # hosted-PR answers a caller keeps between calls
    origin = None
    for full in names:
        remote_ref = full.startswith("refs/remotes/")
        name = full.removeprefix("refs/remotes/" if remote_ref else "refs/heads/")
        if slug not in name.lower() or ref in (name, full) or not valid_ref(full):
            continue
        head, _, rest = name.partition("/")
        short = rest if remote_ref and head in remotes and rest else name
        code, _ = _git(git, repo, ["merge-base", "--is-ancestor",
                                   "--end-of-options", full, target])
        if code == 0:            # already on the ref: finished, not open
            continue
        if code != 1:            # git error: the whole answer is unknown
            return None
        if not remote_ref or head == "origin":
            if origin is None:
                origin = _ok(git, repo, ["remote", "get-url", "origin"]) if "origin" in remotes else ""
                if origin is None:
                    return None
            hosted = _github_repo(origin.strip())
            if hosted:
                tip = _ok(git, repo, ["rev-parse", "--verify", "--end-of-options", full])
                if tip is None or not valid_commit_ref(tip.strip()):
                    return None
                identity = (repo, hosted, short, tip.strip())  # a cache is shared between checkouts and their origins
                if identity not in verified:
                    if frozen:        # a warm answer that no longer matches is unknown, never a forge call
                        return None
                    verified[identity] = _closed_lane_pr(git, repo, hosted, short, tip.strip())
                pr = verified[identity]
                if pr is None:
                    return None
                if pr:
                    closed_prs.add((short, pr))
                    continue
        lanes.setdefault(short, name)
        if len(lanes) >= _LANE_LIMIT:
            break
    if closed is not None:
        closed.extend({"branch": branch, "pr": pr} for branch, pr in sorted(closed_prs)
                      if branch not in lanes)
    return list(lanes.values())


# ------------------------------------------------------------ decisions
_BOOK_LOCK = threading.Lock()
_BOOKS = {}
_BOOK_SLOTS = 3
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]+")


class DecisionsUnreadable(Exception):
    """The decisions authority could not be read for enforcement."""


def decision_book(decisions, *, fresh=False):
    """``{"rows", "superseded", "index"}`` for a :class:`DecisionsAdapter`.

    ``rows`` = ``{"D-n": [row lines]}``, ``superseded`` = :func:`superseded_map`
    and ``index`` = ``{token: {D-ids}}`` over every key-like token, so naming
    lookups cost one dict probe.  Built once per file version (path, inode,
    mtime, size; up to three files kept); an unreadable source is an empty book.
    ``fresh`` bypasses the cache for enforcement without replacing read books.
    """
    if decisions is None:
        return _build_book({})
    if fresh:
        try:
            rows = getattr(decisions, "_rows_strict", getattr(decisions, "_rows", dict))()
        except (OSError, UnicodeDecodeError):
            raise DecisionsUnreadable from None
        return _build_book({did: tuple(lines) for did, lines in rows.items()})
    # Origin-backed rows change independently of the working-tree file's stat.
    path = None if getattr(decisions, "_from_origin", False) else getattr(decisions, "_path", None)
    try:
        stat = os.stat(path) if path is not None else None
    except OSError:
        stat = None
    if stat is None:             # no file identity: parse, but do not cache
        return _build_book(_read_rows(decisions))
    stamp = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    with _BOOK_LOCK:
        book = _BOOKS.get(stamp)
    if book is not None:
        return book
    book = _build_book(_read_rows(decisions))
    with _BOOK_LOCK:
        for old in [k for k in _BOOKS if k[0] == stamp[0]]:
            del _BOOKS[old]
        while len(_BOOKS) >= _BOOK_SLOTS:
            del _BOOKS[next(iter(_BOOKS))]
        _BOOKS[stamp] = book
    return book


def decision_texts(decisions, ids):
    """Nonempty DECISION cells for cited ids; state lives only in decision_book.

    Preserve reference spelling and the adapter's first nonempty, whitespace-
    collapsed cell semantics, including retired decisions. Enforcement must
    continue reading the adapter directly, never this best-effort read view.
    Same-size, same-mtime rewrites may leave decision_book stale; enforcement stays fresh.
    """
    try:
        rows = decision_book(decisions)["rows"]
    except Exception:
        rows = {}
    texts = {}
    for did in ids:
        try:
            if decisions is not None and not isinstance(decisions, DecisionsAdapter):
                text = decisions.text(did)
                if text:
                    texts[did] = text
                continue
            canonical = DecisionsAdapter._canonical(did)
        except Exception:
            continue
        for row in rows.get(canonical, ()):
            try:
                text = re.sub(r"\s+", " ", _decision_cell(row)).strip()
            except Exception:
                continue
            if text:
                texts[did] = text
                break
    return texts


def _read_rows(decisions):
    try:
        return {did: tuple(lines) for did, lines in decisions._rows().items()}
    except Exception:
        return {}


def _build_book(rows):
    index = {}
    for did, lines in rows.items():
        for line in lines:
            for token in _TOKEN_RE.findall(line):
                token = token.strip(".:")
                if "-" in token or ":" in token:     # key-like only
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
