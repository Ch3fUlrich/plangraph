#!/usr/bin/env python3
"""Find public checkouts without the reviewed pre-push gate (standard library)."""
import argparse
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

HOOK = "tools/hooks/pre-push"


def git(path, *args, optional=False):
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True)
    if r.returncode and not (optional and r.returncode == 1):
        raise OSError("git failed")
    return r.stdout


def config(path, *args):
    return git(path, "config", *args, "--get", "core.hooksPath", optional=True).decode().strip()


def public_patterns():
    return [p.strip().lower() for p in
            os.environ.get("PLANGRAPH_PUBLIC_REMOTES", "").split(",") if p.strip()]


def public_remote(path):
    public = public_patterns()
    for name in git(path, "remote").decode().splitlines():
        urls = git(path, "remote", "get-url", "--all", name)
        urls += git(path, "remote", "get-url", "--push", "--all", name)
        for url in urls.decode().splitlines():
            if not public or any(p in url.lower() for p in public):
                # Both URL and scp forms: discard all userinfo, query and fragment.
                if "://" in url:
                    u = urlsplit(url)
                    return (u.hostname or "") + u.path
                return url.rsplit("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    return None


def gated(path, value=None):
    path = Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel")).strip())
    if not git(path, "ls-tree", "HEAD", "--", HOOK):
        return False
    expected = git(path, "show", "HEAD:" + HOOK)
    if value is None:
        value = config(path, "--path")
    if value:
        hook = path / value / "pre-push"
        if hook.is_file() and os.access(hook, os.X_OK):
            if hook.read_bytes() == expected:
                return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[])
    parser.add_argument("--install", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    if args.install and (args.root or args.paths) or args.force and not args.install:
        parser.error("--install accepts only --force")
    if args.install and not public_patterns():
        print("--install requires PLANGRAPH_PUBLIC_REMOTES")
        return 1
    paths = [args.install] if args.install else args.paths[:]
    failed = False
    for root in args.root:
        try:
            paths.extend(p for p in Path(root).iterdir() if (p / ".git").exists())
        except OSError:
            print(f"UNREADABLE {root}")
            failed = True
    if not paths and not args.root:
        paths = [Path.cwd()]
    seen, reported = set(), set()
    for path in paths:
        path = path.absolute()
        if path in seen:
            continue
        seen.add(path)
        try:
            common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            if not args.install:
                records = git(path, "worktree", "list", "--porcelain", "-z").split(b"\0")
                paths.extend(Path(os.fsdecode(r[9:])) for r in records if r.startswith(b"worktree "))
            remote = public_remote(path)
            if not remote:
                if args.install:
                    return 1
                continue
            if args.install:
                if not gated(path, "tools/hooks"):
                    return 1
                old = config(path)
                if old and old != "tools/hooks" and not args.force:
                    return 1
                if args.force and config(path, "--local"):
                    git(path, "config", "--unset-all", "core.hooksPath", optional=True)
                git(path, "config", "core.hooksPath", "tools/hooks")
            elif not gated(path):
                if common not in reported:
                    print(f"NO-GATE {path} {remote}")
                    reported.add(common)
                failed = True
        except (OSError, ValueError, UnicodeError):
            print(f"UNREADABLE {path}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
