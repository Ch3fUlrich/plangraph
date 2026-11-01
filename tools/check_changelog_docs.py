#!/usr/bin/env python3
"""check_changelog_docs.py - Changelog and docs gate (rule D-653).

Guards that changes touching non-doc paths add a line under '## Unreleased' in
CHANGELOG.md, or that every commit touching non-doc paths carries its own
'Changelog: skip (<reason>)' trailer (in the commit's trailer block, merge
commits excluded), and that changes to paths mapped in .docs-map touch at
least one of their documentation files. The range is <merge-base(base, head)>..head.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import os
import re
import subprocess
import sys

BASE_ENV = "CHANGELOG_BASE"
TODAY_ENV = "CHANGELOG_DOCS_TODAY"
SKIP_TRAILER_RE = re.compile(
    r"^\s*changelog\s*:\s*skip(?:\s*\((.*?)\))?\s*$", re.IGNORECASE
)
UNRELEASED_RE = re.compile(r"^##\s+\[?unreleased\]?\s*$", re.IGNORECASE)


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
    """Today, or the injected --today / $CHANGELOG_DOCS_TODAY; ValueError if malformed."""
    today_val = today_arg or os.environ.get(TODAY_ENV)
    if today_val:
        return datetime.date.fromisoformat(today_val.strip())
    return datetime.date.today()


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """'*' and '?' stay inside one path segment; '**' spans segments ('**/' may be empty)."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def unreleased_lines(changelog: str) -> collections.Counter[str]:
    """Non-blank, non-heading lines under '## Unreleased', stripped, as a multiset."""
    found: collections.Counter[str] = collections.Counter()
    inside = False
    for raw in changelog.splitlines():
        line = raw.strip()
        if UNRELEASED_RE.match(line):
            inside = True
        elif raw.startswith("## "):
            inside = False
        elif inside and line and not line.startswith("#"):
            found[line] += 1
    return found


def changelog_entry_added(root: str, merge_base: str, head: str) -> bool:
    """True when head's '## Unreleased' holds a line the merge-base's does not."""
    def read(rev: str) -> str:
        run = git(root, "show", f"{rev}:CHANGELOG.md")
        return run.stdout if run.returncode == 0 else ""

    return bool(unreleased_lines(read(head)) - unreleased_lines(read(merge_base)))


def commit_skips(root: str, sha: str) -> tuple[list[str], int]:
    """(non-empty skip reasons, empty-skip count) from the commit's trailer block only."""
    run = git(root, "log", "-1", "--format=%(trailers:only,unfold)", sha)
    reasons: list[str] = []
    empty = 0
    for line in run.stdout.splitlines():
        m = SKIP_TRAILER_RE.match(line)
        if not m:
            continue
        if m.group(1) is not None and m.group(1).strip():
            reasons.append(m.group(1).strip())
        else:
            empty += 1
    return reasons, empty


def commit_paths(root: str, sha: str) -> list[str]:
    run = git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "--no-renames", sha)
    return [p.strip().replace("\\", "/") for p in run.stdout.splitlines() if p.strip()]


def bail(msg: str, warn_only: bool) -> int:
    print(f"warning: {msg}" if warn_only else msg)
    return 0 if warn_only else 1


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
        "--head",
        help="Head commit or ref to check (default: HEAD); the pre-push hook passes the pushed sha",
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
    warn_only = args.warn_only

    try:
        today = resolve_today(args.today)
    except ValueError:
        bad = args.today or os.environ.get(TODAY_ENV)
        return bail(f"check-changelog-docs: malformed date '{bad}' (want YYYY-MM-DD)", warn_only)

    root = git_toplevel(args.cwd)
    base_ref = args.base or os.environ.get(BASE_ENV) or "origin/main"
    head_ref = args.head or "HEAD"

    base_check = git(root, "rev-parse", "--verify", "--quiet", base_ref + "^{commit}")
    if base_check.returncode != 0:
        return bail(f"check-changelog-docs: base {base_ref} does not resolve", warn_only)
    head_check = git(root, "rev-parse", "--verify", "--quiet", head_ref + "^{commit}")
    if head_check.returncode != 0:
        return bail(f"check-changelog-docs: head {head_ref} does not resolve", warn_only)
    base_sha = base_check.stdout.strip()
    head_sha = head_check.stdout.strip()

    if base_sha == head_sha:
        return bail(
            f"check-changelog-docs: base {base_ref} is {head_ref} itself - cannot diff against {head_ref}",
            warn_only,
        )

    mb_res = git(root, "merge-base", base_sha, head_sha)
    if mb_res.returncode != 0 or not mb_res.stdout.strip():
        return bail(f"check-changelog-docs: no merge-base between {base_ref} and {head_ref}", warn_only)
    merge_base = mb_res.stdout.strip().splitlines()[0]

    diff_res = git(root, "diff", "--name-only", "--no-renames", merge_base, head_sha)
    if diff_res.returncode != 0:
        return bail(f"check-changelog-docs: git diff failed: {diff_res.stderr.strip()}", warn_only)

    raw_paths = [
        p.strip().replace("\\", "/")
        for p in diff_res.stdout.splitlines()
        if p.strip()
    ]

    revs = git(root, "rev-list", "--no-merges", "--reverse", f"{merge_base}..{head_sha}")
    commits = revs.stdout.split() if revs.returncode == 0 else []

    problems: list[str] = []
    warnings: list[str] = []
    skipped: list[str] = []
    uncovered: list[str] = []
    for sha in commits:
        reasons, empty = commit_skips(root, sha)
        touches_non_doc = any(not is_doc_path(p) for p in commit_paths(root, sha))
        if empty:
            problems.append(f"commit {sha[:8]} has 'Changelog: skip' trailer with empty reason")
        if not touches_non_doc:
            continue
        if reasons:
            skipped.append(sha[:8])
            print(f"changelog: skip used by {sha[:8]} ({reasons[0]})")
        else:
            uncovered.append(sha[:8])

    has_non_doc = any(not is_doc_path(p) for p in raw_paths)
    entry_added = changelog_entry_added(root, merge_base, head_sha)

    if entry_added:
        changelog_status = "yes"
    elif has_non_doc and skipped and not uncovered:
        changelog_status = "skip"
    else:
        changelog_status = "no"

    if has_non_doc and changelog_status == "no":
        which = f" (commits without their own skip: {', '.join(uncovered)})" if uncovered else ""
        problems.append(
            "non-doc change requires a line added under '## Unreleased' in CHANGELOG.md, "
            f"or a 'Changelog: skip (<reason>)' trailer on every such commit{which}"
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

        is_enforced = (enforce_after is None) or (today >= enforce_after)

        compiled = [(glob_to_regex(g), g, d) for _, g, d in rules]
        for path in raw_paths:
            for rx, glob_pat, target_docs in compiled:
                if rx.match(path):
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

    if warn_only:
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
