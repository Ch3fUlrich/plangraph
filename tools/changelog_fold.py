#!/usr/bin/env python3
"""changelog_fold.py - fold changelog.d fragments into CHANGELOG.md (D-3732).

Each fragment holds entry lines in the form used under '## Unreleased'. Fold
inserts entry lines under '## Unreleased': uncommitted fragments first by
mtime, then committed fragments by first-commit date, newest first with filename
ties. Only folded fragments are deleted; empty fragments remain and are reported. A second run changes nothing; --check writes nothing and exits
1 while fragments remain.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FRAGMENT_DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
UNRELEASED_RE = re.compile(r"^##\s+\[?unreleased\]?\s*$", re.IGNORECASE)


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root)] + list(args),
        capture_output=True,
        text=True,
        errors="replace",
    )


def repo_root(cwd: str) -> Path:
    run = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if run.returncode != 0:
        raise SystemExit("changelog-fold: the cwd is not in a git checkout")
    return Path(run.stdout.strip())


def fragments(root: Path) -> list[Path]:
    """*.md files directly under changelog.d, excluding the README."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.glob("*.md")
        if path.is_file() and path.name.lower() != "readme.md"
    )


def first_commit_date(root: Path, path: Path) -> tuple[int, float]:
    """Uncommitted fragments are newest by mtime; otherwise use first commit."""
    rel = path.relative_to(root).as_posix()
    run = git(root, "log", "--diff-filter=A", "--format=%ct", "--", rel)
    stamps = [int(value) for value in run.stdout.split() if value.isdigit()]
    changed = git(root, "status", "--porcelain", "--", rel).stdout.strip()
    return (1, path.stat().st_mtime_ns) if changed or not stamps else (0, min(stamps))


def entry_lines(path: Path) -> list[str]:
    """Non-blank, non-heading lines; an empty fragment yields none."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def fold_text(changelog: str, entries: list[str]) -> str:
    """Insert entries directly under '## Unreleased'; ValueError without that heading."""
    lines = changelog.splitlines()
    for index, raw in enumerate(lines):
        if UNRELEASED_RE.match(raw.strip()):
            return "\n".join(lines[:index + 1] + entries + lines[index + 1:]) + "\n"
    raise ValueError(f"{CHANGELOG} has no '## Unreleased' heading")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="changelog-fold",
        description="Fold changelog.d fragments into CHANGELOG.md (D-3732).",
    )
    ap.add_argument("--check", action="store_true", help="report pending fragments without writing")
    ap.add_argument("--cwd", default=".", help="git working tree root (defaults to cwd)")
    args = ap.parse_args(argv)

    root = repo_root(args.cwd)
    pending = fragments(root)
    if not pending:
        print("changelog-fold: no fragments")
        return 0

    items = []
    for path in pending:
        lines = entry_lines(path)
        if lines:
            items.append((first_commit_date(root, path), path.name, lines))
        else:
            print(f"changelog-fold: no entry lines; leaving {path.relative_to(root)}")
    items.sort(key=lambda item: (-item[0][0], -item[0][1], item[1]))
    entries = [line for _, _, block in items for line in block]

    changelog_path = root / CHANGELOG
    if not changelog_path.is_file():
        raise SystemExit(f"changelog-fold: {CHANGELOG} is missing")
    original = changelog_path.read_text(encoding="utf-8")
    try:
        updated = fold_text(original, entries)
    except ValueError as exc:
        raise SystemExit(f"changelog-fold: {exc}")

    if args.check:
        print(f"changelog-fold: {len(pending)} fragment(s) pending")
        return 1

    if entries:
        changelog_path.write_text(updated, encoding="utf-8")
    for _, name, _ in items:
        (root / FRAGMENT_DIR / name).unlink()
    print(f"changelog-fold: folded {len(items)} fragment(s), deleted {len(items)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
