"""Authoritative hosted PR lookup and commit+ci provenance validation."""
from __future__ import annotations

import json
import subprocess
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
        lookup = getattr(ctx.ci, "merged_pr_for", None)
        proven = valid_commit_ref(merge_sha) and callable(lookup) and lookup(
            project, merge_sha, target_ref)
        if proven:
            return None
    except Exception:  # Never expose sensitive text from adapter errors.
        pass
    try:
        if (ctx.ci.mode(project) or "").startswith("forgejo:"):
            return Result(False, "merged_pr_unproven: forgejo has no merged PR lookup", ref)
    except Exception:
        pass
    return Result(False, "merged_pr_unproven: no authoritative merged PR association", ref)
