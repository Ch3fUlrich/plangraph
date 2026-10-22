"""CI commit trailer gate: Writer attribution and test-removal reasons.

Commits come from ``git log --no-merges base..head``, so merge commits are never
inspected.  Commits listed in the exempt file skip every rule.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Trailer key/value per message line; the last ``Writer:`` line wins.
WRITER_RE = re.compile(r"^Writer:\s*(.+)$")

#: A test-removal commit must carry ``Test-Removal: D-<digits>``.
TEST_REMOVAL_RE = re.compile(r"^Test-Removal:\s*D-\d+")

#: Case-insensitive markers in the Writer value that identify a Claude writer.
CLAUDE_MARKERS: tuple[str, ...] = (
    "claude",
    "anthropic",
    "sonnet",
    "opus",
    "haiku",
    "fable",
)

#: Deleted file names matching ``tests/test_*.py``.
TEST_PATH_RE = re.compile(r"^tests/test_.*\.py$")

#: Removed diff lines (``-U0``) defining a test function.
REMOVED_TEST_DEF_RE = re.compile(r"^-\s*(async\s+)?def test_")

EXEMPT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

MISSING_WRITER = "missing Writer trailer"
CLAUDE_WRITER = "claude writer"
TEST_REMOVAL_VIOLATION = "test removal without Test-Removal trailer"

#: Default exempt file lives next to this script, so the CLI works from any cwd.
DEFAULT_EXEMPT = Path(__file__).resolve().with_name("ci_commit_exempt.txt")


class ExemptError(Exception):
    """The exempt file is missing or malformed."""


class GateError(Exception):
    """Git itself failed, so the range could not be inspected."""


def load_exempt(path: str | Path) -> set[str]:
    """Parse the exempt file: 40-hex shas, ``#`` comments and blanks ignored."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ExemptError(f"cannot read exempt file {path}: {exc}") from exc

    exempt: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if EXEMPT_SHA_RE.match(line) is None:
            raise ExemptError(f"exempt file {path}:{lineno}: not a 40-hex sha: {line!r}")
        exempt.add(line.lower())
    return exempt


def _git_text(args: list[str], cwd: str | Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise GateError(f"git {args[0]} failed: {detail[0] if detail else 'unknown error'}")
    return proc.stdout.decode("utf-8", errors="replace")


def _iter_commits(base: str, head: str, cwd: str | Path | None = None) -> list[tuple[str, str]]:
    """Return ``(sha, message)`` for ``git log --no-merges base..head`` (newest first)."""
    out = _git_text(
        ["log", "--no-merges", "-z", "--format=%H%n%B", f"{base}..{head}"], cwd=cwd
    )
    commits: list[tuple[str, str]] = []
    for record in out.split("\0"):
        if not record.strip():
            continue
        sha, _, message = record.partition("\n")
        commits.append((sha.strip(), message))
    return commits


def _writer_value(message: str) -> str | None:
    """Last ``Writer:`` line wins; ``None`` when the trailer is absent."""
    value: str | None = None
    for line in message.splitlines():
        match = WRITER_RE.match(line)
        if match:
            value = match.group(1).strip()
    return value


def _has_test_removal_trailer(message: str) -> bool:
    return any(TEST_REMOVAL_RE.match(line) for line in message.splitlines())


def _removes_test(sha: str, cwd: str | Path | None = None) -> bool:
    """True when the commit deletes ``tests/test_*.py`` or drops a ``def test_`` line."""
    deleted = _git_text(["show", "--format=", "--diff-filter=D", "--name-only", sha], cwd=cwd)
    for line in deleted.splitlines():
        if TEST_PATH_RE.match(line.strip()):
            return True

    diff = _git_text(["show", "--format=", "-U0", sha, "--", "tests"], cwd=cwd)
    for line in diff.splitlines():
        if REMOVED_TEST_DEF_RE.match(line):
            return True
    return False


def check(
    base: str,
    head: str,
    exempt: str | Path | None = None,
    cwd: str | Path | None = None,
) -> list[str]:
    """Return one ``sha40: reason`` line per violation in ``base..head``.

    Raises ``ExemptError`` for a bad exempt file and ``GateError`` when git fails.
    """
    exempt_set = load_exempt(exempt) if exempt else set()

    violations: list[str] = []
    for sha, message in _iter_commits(base, head, cwd=cwd):
        if sha in exempt_set:
            continue

        writer = _writer_value(message)
        if writer is None:
            violations.append(f"{sha}: {MISSING_WRITER}")
        elif any(marker in writer.lower() for marker in CLAUDE_MARKERS):
            violations.append(f"{sha}: {CLAUDE_WRITER}")

        # A present Test-Removal trailer satisfies the rule, so git only runs when absent.
        if not _has_test_removal_trailer(message) and _removes_test(sha, cwd=cwd):
            violations.append(f"{sha}: {TEST_REMOVAL_VIOLATION}")

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI commit trailer gate")
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--base", required=True, help="Base commit SHA")
    check_parser.add_argument("--head", required=True, help="Head commit SHA")
    check_parser.add_argument(
        "--exempt",
        default=None,
        help="Exempt sha file (default: tools/ci_commit_exempt.txt next to this script)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage; keep main() return-based
        return int(exc.code or 0)

    if args.command != "check":
        parser.print_help(sys.stderr)
        return 2

    exempt = args.exempt if args.exempt is not None else DEFAULT_EXEMPT
    try:
        violations = check(args.base, args.head, exempt=exempt, cwd=None)
    except (ExemptError, GateError) as exc:
        sys.stderr.write(f"ci-commit-gate: {exc}\n")
        return 2

    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
