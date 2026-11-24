"""Authoritative hosted PR lookup and commit+ci provenance validation."""
from __future__ import annotations

import json
import subprocess
from bisect import bisect_left
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from .evidence_types import Result, valid_commit_ref


def merged_pr_for(adapter, project, merge_sha, target_ref):
    """Read every GitHub association page; partial/error responses never prove PRs.

    gh follows pagination to completion; the subprocess deadline bounds the whole
    lookup, not each page. All pages are validated before considering any match.
    """
    if not valid_commit_ref(merge_sha):
        raise ValueError("PR lookup requires a full lowercase commit sha")
    mode = adapter.mode(project)
    if not mode or mode == "none" or mode.startswith("forgejo:"):
        return None
    if Path(mode).is_dir():
        origin = subprocess.run(
            [adapter._git, "-C", mode, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=adapter._timeout, check=False,
        )
        from urllib.parse import urlsplit
        remote = origin.stdout.strip()
        github = (remote.startswith("git" + "@" + "github.com:") or
                  urlsplit(remote).hostname == "github.com")
        if origin.returncode != 0 or not github:
            return None
    owner, repo = adapter._validated_slug(adapter._slug(mode))
    if owner in (".", "..") or repo in (".", ".."):
        raise ValueError("invalid repository identity")
    slug = f"{owner}/{repo}"
    branch = target_ref
    if branch.startswith("refs/remotes/"):
        branch = branch[len("refs/remotes/"):].split("/", 1)[-1]
    elif branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/"):]
    elif "/" in branch:
        # Only strip an actual configured checkout remote, not a branch prefix.
        if Path(mode).is_dir():
            remotes = subprocess.run(
                [adapter._git, "-C", mode, "remote"], capture_output=True,
                text=True, timeout=adapter._timeout, check=False,
            )
            if remotes.returncode != 0:
                raise RuntimeError("cannot resolve target remote")
            prefix, rest = branch.split("/", 1)
            if prefix in remotes.stdout.splitlines():
                branch = rest
        elif branch.startswith("origin/"):
            branch = branch[len("origin/"):]
    path = "repos/{}/{}/commits/{}/pulls".format(
        quote(owner, safe=""), quote(repo, safe=""), quote(merge_sha, safe=""))
    proc = subprocess.run(
        [adapter._gh, "api", "--paginate", path],
        capture_output=True, text=True, timeout=adapter._timeout, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("PR lookup failed")
    # gh emits one array per page, without an enclosing array (even on older gh).
    decoder = json.JSONDecoder()
    pages = []
    offset = 0
    while offset < len(proc.stdout):
        if proc.stdout[offset] in " \t\r\n":
            offset += 1
            continue
        try:
            page, offset = decoder.raw_decode(proc.stdout, offset)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed PR pages") from exc
        if not isinstance(page, list) or any(not isinstance(p, Mapping) for p in page):
            raise ValueError("malformed PR pages")
        pages.append(page)
    if not pages:
        raise ValueError("malformed PR pages")
    for page in pages:
        for pr in page:
            base = pr.get("base")
            base = base if isinstance(base, Mapping) else {}
            repository = base.get("repo")
            repository = repository if isinstance(repository, Mapping) else {}
            merged = pr.get("merged_at")
            if (isinstance(merged, str) and merged.strip()
                    and pr.get("merge_commit_sha") == merge_sha
                    and repository.get("full_name") == slug
                    and base.get("ref") == branch):
                return pr
    return None


def landing_record(ci, project, merge_sha, target_ref):
    """The forge's record of one landing and its pre-merge required checks, or ``None``.

    The merged PR is found by its ``merge_commit_sha``, so a merge commit and a squash merge
    resolve alike; the squash commit is not the PR head, which the forge reports as ``head.sha``.
    ``checks`` holds the CI verdict of that PR head, judged by the same newest-run-per-group
    rule as the ``commit+ci`` verifier: ``verified`` is true only when it is green.  A forge
    error on the check-runs leaves ``checks`` unverified with a fixed reason; it never raises
    for them.  Returns ``None`` when the forge names no merged PR for the commit.
    """
    pr = ci.merged_pr_for(project, merge_sha, target_ref)
    if not isinstance(pr, Mapping):
        return None
    head_ref = pr.get("head")
    head = head_ref.get("sha") if isinstance(head_ref, Mapping) else None
    number = pr.get("number")
    record = {"pr": number if type(number) is int else None, "merge_sha": merge_sha,
              "head": head if valid_commit_ref(head) else None,
              "merged_at": pr.get("merged_at"),
              "checks": {"head": None, "verified": False, "detail": "pr_head_unknown", "names": []}}
    if record["head"] is None:
        return record
    from .evidence import _ci_conclusion

    try:
        runs = ci.check_runs(ci.mode(project), record["head"])
        verdict = _ci_conclusion(runs, record["head"])
        names = sorted({str(r.get("name")) for r in runs if isinstance(r, Mapping) and r.get("name")})
        record["checks"] = {"head": record["head"], "verified": verdict is None,
                            "detail": verdict or "CI green", "names": names}
    except Exception:  # noqa: BLE001 - adapter text may hold paths or tokens
        record["checks"] = {"head": record["head"], "verified": False,
                            "detail": "check_runs_unavailable", "names": []}
    return record


def non_pr_merge(ctx, project, merge_sha, target_ref):
    """Opt-in local topology proof; repository policy is router-owned config."""
    from .evidence_git import valid_ref

    entries = getattr(ctx.git, "_repos", {})
    entry = entries.get(project) if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("non_pr") is not True:
        return False
    if not valid_commit_ref(merge_sha) or not valid_ref(target_ref):
        return False
    configured_ref = entry.get("ref")
    if not valid_ref(configured_ref) or target_ref != configured_ref:
        return False
    # This exception proves only merge provenance, never a CI exemption.
    if ctx.ci.mode(project) in (None, "none", ""):
        return False
    repo = ctx.git.repo_path(project)
    parents = ctx.git._run(
        repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", merge_sha])
    tokens = parents.stdout.split()
    return (parents.returncode == 0 and len(tokens) >= 3
            and tokens[0] == merge_sha
            and all(valid_commit_ref(parent) for parent in tokens[1:])
            and ctx.git.is_ancestor(repo, merge_sha, target_ref))


def validate_merge(ctx, project, merge_info, target_ref, ref):
    """Preserve principal policy, then require proof from the injected adapter."""
    merge_sha, email = merge_info
    principals = getattr(ctx, "merge_principals", ())
    if principals:
        allowed = {p.lower() for p in principals if isinstance(p, str)}
        if email.lower() not in allowed:
            return Result(False,
                          f"merge commit committer {email!r} is not an allowed merge principal", ref)
    try:
        proven = non_pr_merge(ctx, project, merge_sha, target_ref)
        lookup = getattr(ctx.ci, "merged_pr_for", None)
        if not proven:
            proven = valid_commit_ref(merge_sha) and callable(lookup) and lookup(
                project, merge_sha, target_ref)
        if proven:
            # Provenance only: the caller still enforces floor and CI guards.
            return None
    except Exception:  # Never expose sensitive text from adapter errors.
        pass
    try:
        if (ctx.ci.mode(project) or "").startswith("forgejo:"):
            return Result(False, "merged_pr_unproven: forgejo has no merged PR lookup", ref)
    except Exception:
        pass
    return Result(False, "merged_pr_unproven: no authoritative merged PR association", ref)


#: Work under this prefix only records items as deferred; it delivers nothing.
CHECKPOINT_PREFIX = "docs/checkpoints/"


def indexed_revision(index, token):
    """Resolve a revision prefix when it identifies exactly one indexed commit."""
    ordered = index.get("revision_order", ())
    offset = bisect_left(ordered, token)
    matches = [sha for sha in ordered[offset:offset + 2] if sha.startswith(token)]
    return matches[0] if len(matches) == 1 else None


def landed_go_part(index, head, token, pr):
    """Resolve an unambiguous part introduced with this default-branch GO head."""
    graph = index.get("merged_history", {})
    part = indexed_revision(index, token)
    if not part or part == head:
        return None
    landed = graph.get(head, ((), None))[1]
    candidate = graph.get(part, ((), None))[1]
    merge = (landed or {}).get("merge") or {}
    if not candidate or merge.get("pr") != pr or candidate.get("merge") != merge:
        return None
    seen, todo = set(), [head]
    while todo:
        sha = todo.pop()
        if sha == part:
            return candidate
        if sha not in seen:
            seen.add(sha)
            todo.extend(graph.get(sha, ((), None))[0])


def landed_deploy_work(git, repo, parent, tip, merge, key, *, train):
    """Use the landed diff because a topic's first-parent diff can include main syncs."""
    names = {key, key.rsplit(":", 1)[-1]}
    changes = part_changes(git, repo, parent, tip, merge, names, train_resolution=train)
    parts = list(git._train_deploy_changes(repo, parent, tip, names)) if train else []
    changes.extend(change for _, _, paths in parts for change in paths)
    if train:
        # A unit one part adds and a later part removes is not delivered: judge each part's paths against
        # what the whole train nets out to, as the deploy plan does.
        net = _train_net_paths(git, repo, parent, tip)
        if net is not None:
            changes = [change for change in changes if change[1] in net]
            parts = [(part, part_merge, [change for change in paths if change[1] in net])
                     for part, part_merge, paths in parts]
    return changes, parts


def _train_net_paths(git, repo, parent, tip):
    """Paths the train changes against its base (``None`` when git cannot say), deleted ones excluded."""
    base = git._run(repo, ["merge-base", "--end-of-options", parent, tip])
    if base.returncode != 0 or not valid_commit_ref(base.stdout.strip()):
        return None
    try:
        return {path for status, path in git._name_status(repo, ["--end-of-options", base.stdout.strip(), tip])
                if status != "D"}
    except RuntimeError:
        return None


def part_changes(git, repo, parent, part, merge, names, *, train_resolution=False):
    """Attribute landed paths to an item with their net status against the landing base."""
    from .evidence_git import plan_items
    scope = ["--first-parent"] if train_resolution else []
    proc = git._run(repo, ["log", *scope, "--format=%H %P%x1e%B%x1d",
                          "--end-of-options", part, f"^{parent}"])
    if proc.returncode != 0:
        raise RuntimeError("cannot read landed commits")
    own, untagged, own_merges, tagged = [], [], [merge], False
    for record in (proc.stdout or "").split("\x1d"):
        header, _, body = record.strip().partition("\x1e")
        commits = header.split()
        if not commits or not valid_commit_ref(commits[0]):
            continue
        sha, *parents = commits
        items = plan_items(body)
        tagged = tagged or bool(items)
        if names.intersection(items):
            (own_merges if len(parents) > 1 else own).append(sha)
        elif not items and len(parents) < 2:
            untagged.append(sha)
    net = git._name_status(repo, ["--end-of-options", parent, merge])
    if not train_resolution and not tagged:
        # Legacy and explicitly named deliveries can have only untagged topic commits.
        return net
    # An untagged follow-up belongs to a part with work for the key. The
    # aggregate train's trailers also list keys absent from its delivered parts.
    has_own_work = own or len(own_merges) > 1
    shas = own + untagged if has_own_work and not train_resolution else own
    touched = {path for sha in shas for _, path in git._name_status(repo, ["--root", "--end-of-options", sha])}
    for sha in own_merges:
        # A merge changes only paths whose result differs from all its parents.
        diff = git._run(repo, ["diff-tree", "--no-commit-id", "--name-only", "-c", "-r", "-z",
                               "--end-of-options", sha])
        if diff.returncode != 0:
            raise RuntimeError("cannot read the landed merge")
        touched.update(path for path in diff.stdout.split("\0") if path)
    return sorted(change for change in net if change[1] in touched)


def _cancelled(git, repo, head, target):
    """Commits a revert of *target* undoes: those its part merge introduced, else *target* alone."""
    def introduced(commit):
        proc = git._run(repo, ["rev-list", "--end-of-options", commit, f"^{commit}^1"])
        return set(proc.stdout.split()) if proc.returncode == 0 else {commit}

    proc = git._run(repo, ["rev-list", "--first-parent", "--merges", "--reverse", "--ancestry-path",
                           "--end-of-options", f"{target}..{head}"])
    for merge in [target, *(proc.stdout.split() if proc.returncode == 0 else [])]:
        parents = git._run(repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", merge]).stdout.split()
        if len(parents) > 2 and (merge == target or git._run(
                repo, ["merge-base", "--is-ancestor", target, parents[2]]).returncode == 0):
            return introduced(merge)
    return {target}


def train_trailer_keys(git, repo, head, exclude=()):
    """``(delivered, deferred)`` ``Plan-Item`` keys of the commits in *head* not in any *exclude* ref.

    Commits are read oldest first: a revert or drop takes back the part it cancels (a key it names
    with no target, all of that key), and a later commit delivers the key again. A key is deferred
    when every commit still delivering it is a non-merge commit touching only ``docs/checkpoints/**``.
    """
    from .evidence_git import delivery_revert, plan_items, valid_ref
    if not all(isinstance(r, str) and valid_ref(r) for r in (head, *exclude)):
        raise ValueError("invalid ref")
    proc = git._run(repo, ["log", "--reverse", "--topo-order", "--format=%H%x1e%P%x1e%B%x1d",
                           "--end-of-options", head, *[f"^{r}" for r in exclude]])
    if proc.returncode != 0:
        raise RuntimeError("cannot read the train commits")
    live, seen = {}, []
    for record in (proc.stdout or "").split("\x1d"):
        fields = record.strip("\n").split("\x1e", 2)
        if len(fields) != 3 or not valid_commit_ref(fields[0]):
            continue
        sha, parents, body = fields
        seen.append(sha)
        if (cancel := delivery_revert(body)) is not None:
            target = next((s for s in seen if cancel[0] and s.startswith(cancel[0])), None)
            undone = _cancelled(git, repo, head, target) if target else set()
            for key in list(live):
                if target:
                    live[key] = {s: v for s, v in live[key].items() if s not in undone}
                elif key in cancel[1]:
                    live[key] = {}
            live = {key: shas for key, shas in live.items() if shas}
        elif keys := plan_items(body):
            merged = len(parents.split()) > 1
            paths = [] if merged else [path for _, path in git._name_status(repo, ["--root", "--end-of-options", sha])]
            only = bool(paths) and all(path.startswith(CHECKPOINT_PREFIX) for path in paths)
            for key in keys:
                live.setdefault(key, {})[sha] = only
    deferred = sorted(key for key, shas in live.items() if all(shas.values()))
    return sorted(set(live) - set(deferred)), deferred
