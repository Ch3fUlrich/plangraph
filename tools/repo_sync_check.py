#!/usr/bin/env python3
"""Fetch repositories and fast-forward only clean default-branch checkouts.

Dry runs still fetch, but only report eligible updates. Fetch metadata may change
on ALERT; checkout contents, HEAD, local branches and the index are never repaired.
"""
import argparse
from pathlib import Path
import subprocess


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
         "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false",
         "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace", check=check,
        timeout=60 if args[0] == "fetch" else None)


def token(value):
    """Keep repository labels on one line, even for unusual directory names."""
    return "".join(c if c.isprintable() and not c.isspace() else "_" for c in value)


def check_repo(repo, dry_run=False, verbose=False):
    behind = 0

    def alert(reason):
        print(f"ALERT {token(repo.name)} behind={behind} reason={reason}")
        return 1

    try:
        git(repo, "fetch", "-q", "origin")
        ref = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD",
                  check=False).stdout.strip()
        default = ref.removeprefix("refs/remotes/origin/") if ref else "main"
        target = "refs/remotes/origin/" + default
        # Pin the fetched commit; a concurrent fetch must not move our target.
        tip = git(repo, "rev-parse", "--verify", target + "^{commit}").stdout.strip()
        behind = int(git(repo, "rev-list", "--count", f"HEAD..{tip}").stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return alert("fetch-failed")
    if not behind:
        if verbose:
            print(f"OK {token(repo.name)}")
        return 0
    try:
        branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if branch.returncode == 1:
            return alert("detached")
        branch.check_returncode()
        if branch.stdout.strip() != default:
            return alert("other-branch:" + token(branch.stdout.strip()))
        if git(repo, "status", "--porcelain", "--untracked-files=no",
               "--ignore-submodules=untracked").stdout:
            return alert("tracked-changes")
        if git(repo, "merge-base", "--is-ancestor", "HEAD", tip,
               check=False).returncode:
            return alert("diverged")
        # Refuse collisions before merge can write ORIG_HEAD, even on failure.
        git(repo, "read-tree", "--dry-run", "-m", "-u", "HEAD", tip)
        added = git(repo, "diff", "--name-only", "--no-renames", "--diff-filter=A",
                    "-z", "HEAD", tip).stdout.split("\0")[:-1]
        untracked = git(repo, "ls-files", "--others", "-z").stdout.split("\0")[:-1]
        if any(p == a or p.startswith(a + "/") or a.startswith(p.rstrip("/") + "/")
               for p in untracked for a in added):
            return alert("diverged")
        if dry_run:
            print(f"DRY-RUN {token(repo.name)} behind={behind}")
        else:
            git(repo, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", tip)
    except (OSError, subprocess.SubprocessError):
        return alert("diverged")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, default=[],
                        help="scan direct children of this directory (repeatable)")
    parser.add_argument("--repo", action="append", type=Path, default=[],
                        help="check this repository (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="fetch but do not merge")
    parser.add_argument("--verbose", action="store_true", help="also print up-to-date repos")
    args = parser.parse_args(argv)
    roots = args.root or ([] if args.repo else [Path.home() / "code"])
    repos = list(args.repo)
    failed = 0
    for root in roots:
        try:
            repos.extend(p for p in sorted(root.expanduser().iterdir())
                         if p.is_dir() and (p / ".git").exists()
                         and not p.name.endswith(("-worktrees", "-lanes")))
        except OSError:
            print(f"ALERT {token(root.name)} behind=0 reason=fetch-failed")
            failed = 1
    seen = set()
    for repo in repos:
        repo = repo.expanduser().absolute()
        key = repo.resolve()
        if key not in seen:
            seen.add(key)
            failed |= check_repo(repo, args.dry_run, args.verbose)
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
