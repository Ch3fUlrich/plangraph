#!/usr/bin/env python3
"""check_changelog_docs.py - Changelog and docs gate (rule D-653).

Guards that changes touching non-doc paths include a CHANGELOG.md hunk or a
commit trailer 'Changelog: skip (<reason>)', and that changes to paths mapped
in .docs-map touch at least one of their documentation files.
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import os
import re
import subprocess
import sys

BASE_ENV = "CHANGELOG_BASE"
TODAY_ENV = "CHANGELOG_DOCS_TODAY"
SKIP_TRAILER_RE = re.compile(
    r"^\s*changelog\s*:\s*skip(?:\s*\((.*?)\))?\s*$", re.IGNORECASE
)


def git(root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root)] + list(args),
        capture_output=True,
        text=True,
        errors="replace",
    )


def git_toplevel(cwd: str | None = None) -> str:
    cmd = ["git"]
    if cwd:
        cmd.extend(["-C", str(cwd)])
    cmd.extend(["rev-parse", "--show-toplevel"])
    run = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if run.returncode != 0:
        raise SystemExit(
            "check-changelog-docs: the cwd is not in a git checkout "
            "(run inside one; the gate measures the toplevel)"
        )
    return os.path.abspath(run.stdout.strip())


def is_doc_path(path: str) -> bool:
    """Doc paths (never need a changelog): *.md, docs/**, LICENSE, .docs-map."""
    norm = path.replace("\\", "/")
    if norm.endswith(".md"):
        return True
    if norm == "docs" or norm.startswith("docs/"):
        return True
    if norm == "LICENSE" or norm == ".docs-map":
        return True
    return False


def parse_docs_map(path: str) -> tuple[datetime.date | None, list[tuple[int, str, list[str]]], list[str]]:
    """Parse .docs-map: (enforce_after_date, rules, parse_errors)."""
    enforce_after: datetime.date | None = None
    rules: list[tuple[int, str, list[str]]] = []
    errors: list[str] = []
    filename = os.path.basename(path)

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        errors.append(f"{filename}: cannot read file: {exc}")
        return None, rules, errors

    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"^#\s*enforce-after:\s*(\S+)", line)
            if m:
                d_str = m.group(1).strip()
                try:
                    enforce_after = datetime.date.fromisoformat(d_str)
                except ValueError:
                    errors.append(f"{filename}:{lineno}: malformed enforce-after date: '{d_str}'")
            continue
        if " -> " not in line:
            errors.append(f"{filename}:{lineno}: malformed rule: want '<glob> -> <doc>', got: '{line}'")
            continue
        glob_part, _, docs_part = line.partition(" -> ")
        glob_part = glob_part.strip()
        docs = [d.strip() for d in docs_part.split(",") if d.strip()]
        if not glob_part or not docs:
            errors.append(f"{filename}:{lineno}: malformed rule: want '<glob> -> <doc>', got: '{line}'")
            continue
        rules.append((lineno, glob_part, docs))

    return enforce_after, rules, errors


def resolve_today(today_arg: str | None = None) -> datetime.date:
    today_val = today_arg or os.environ.get(TODAY_ENV)
    if today_val:
        return datetime.date.fromisoformat(today_val.strip())
    return datetime.date.today()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check-changelog-docs",
        description="Verify CHANGELOG.md and docs updates for changed paths (D-653).",
    )
    ap.add_argument(
        "--base",
        help="Base commit or ref to diff against (default: $CHANGELOG_BASE or origin/main)",
    )
    ap.add_argument(
        "--warn-only",
        action="store_true",
        help="Demote all errors to warnings and exit 0 (used by pre-push hook)",
    )
    ap.add_argument(
        "--today",
        help="Override current date (YYYY-MM-DD) for docs-map enforcement checks",
    )
    ap.add_argument(
        "--cwd",
        help="Git working tree root to check (defaults to cwd)",
    )
    args = ap.parse_args(argv)

    root = git_toplevel(args.cwd)
    required = args.base or os.environ.get(BASE_ENV)
    base_ref = required if required else "origin/main"

    base_check = git(root, "rev-parse", "--verify", "--quiet", base_ref + "^{commit}")
    if base_check.returncode != 0:
        msg = f"check-changelog-docs: base {base_ref} does not resolve"
        if args.warn_only:
            print(f"warning: {msg}")
            return 0
        print(msg)
        return 1

    head_check = git(root, "rev-parse", "HEAD")
    if base_check.stdout.strip() == head_check.stdout.strip():
        msg = f"check-changelog-docs: base {base_ref} is HEAD itself - cannot diff against HEAD"
        if args.warn_only:
            print(f"warning: {msg}")
            return 0
        print(msg)
        return 1

    diff_res = git(root, "diff", "--name-only", "--no-renames", f"{base_ref}...HEAD")
    if diff_res.returncode != 0:
        msg = f"check-changelog-docs: git diff failed: {diff_res.stderr.strip()}"
        if args.warn_only:
            print(f"warning: {msg}")
            return 0
        print(msg)
        return 1

    raw_paths = [
        p.strip().replace("\\", "/")
        for p in diff_res.stdout.splitlines()
        if p.strip()
    ]

    log_res = git(root, "log", "-z", "--format=%H%n%B", f"{base_ref}..HEAD")
    commits: list[tuple[str, str]] = []
    if log_res.returncode == 0 and log_res.stdout:
        for rec in log_res.stdout.split("\0"):
            if not rec.strip():
                continue
            sha, _, message = rec.partition("\n")
            commits.append((sha.strip(), message))

    valid_skips: list[tuple[str, str]] = []
    empty_skips: list[str] = []
    for sha, message in commits:
        for line in message.splitlines():
            m = SKIP_TRAILER_RE.match(line)
            if m:
                reason = m.group(1)
                if reason is not None and reason.strip():
                    valid_skips.append((sha[:8], reason.strip()))
                    break
                else:
                    empty_skips.append(sha[:8])
                    break

    for sha, reason in valid_skips:
        print(f"changelog: skip used by {sha} ({reason})")

    problems: list[str] = []
    warnings: list[str] = []

    for sha in empty_skips:
        problems.append(f"commit {sha} has 'Changelog: skip' trailer with empty reason")

    has_non_doc = any(not is_doc_path(p) for p in raw_paths)
    changelog_in_diff = "CHANGELOG.md" in raw_paths

    if changelog_in_diff:
        changelog_status = "yes"
    elif valid_skips:
        changelog_status = "skip"
    else:
        changelog_status = "no"

    if has_non_doc and not changelog_in_diff and not valid_skips:
        problems.append(
            "non-doc change requires CHANGELOG.md or 'Changelog: skip (<reason>)' commit trailer"
        )

    # Rule B: .docs-map
    docs_map_file = os.path.join(root, ".docs-map")
    docs_map_warnings = 0
    docs_map_errors = 0

    if os.path.isfile(docs_map_file):
        enforce_after, rules, parse_errors = parse_docs_map(docs_map_file)
        for err in parse_errors:
            problems.append(err)
            docs_map_errors += 1

        today = resolve_today(args.today)
        is_enforced = (enforce_after is None) or (today >= enforce_after)

        for path in raw_paths:
            for lineno, glob_pat, target_docs in rules:
                if fnmatch.fnmatch(path, glob_pat):
                    if not any(d in raw_paths for d in target_docs):
                        docs_str = ", ".join(target_docs)
                        if is_enforced:
                            docs_map_errors += 1
                            problems.append(
                                f"{path} matches '{glob_pat}' but none of its docs ({docs_str}) are in diff"
                            )
                        else:
                            docs_map_warnings += 1
                            warnings.append(
                                f"warning: {path} matches '{glob_pat}' but none of its docs ({docs_str}) are in diff"
                            )

    if args.warn_only:
        for p in problems:
            print(f"warning: {p}")
        for w in warnings:
            print(w)
        docs_map_warnings += docs_map_errors
        docs_map_errors = 0
        summary_verdict = "ok"
        exit_code = 0
    else:
        for p in problems:
            print(f"FAIL: {p}")
        for w in warnings:
            print(w)
        summary_verdict = "ok" if not problems else "FAIL"
        exit_code = 0 if not problems else 1

    print(
        f"check-changelog-docs: {summary_verdict} - {len(raw_paths)} path(s), "
        f"changelog {changelog_status}, docs-map {docs_map_warnings} warning(s)/{docs_map_errors} error(s)"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
