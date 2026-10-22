"""CI commit trailer gate: Writer attribution and test-removal reasons.

Commits come from ``git log --no-merges base..head``, so merge commits are never
inspected.  Commits listed in the exempt file skip every rule.  A repo-relative
exempt path is read from the BASE commit, never from the working tree, so a PR
cannot exempt the commits it introduces.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Trailer key/value per message line; every ``writer:`` line is inspected
#: (case-insensitive), and no line is allowed to override an earlier one.
WRITER_RE = re.compile(r"^writer:\s*(.*)$", re.IGNORECASE)

#: A test-removal commit must carry ``Test-Removal: D-<digits>`` and nothing after.
TEST_REMOVAL_RE = re.compile(r"^Test-Removal:\s*D-\d+\s*$")

#: Case-insensitive markers in the Writer value that identify a Claude writer.
CLAUDE_MARKERS: tuple[str, ...] = (
    "claude",
    "anthropic",
    "sonnet",
    "opus",
    "haiku",
    "fable",
)

#: Test file paths: anything under a ``test``/``tests`` directory, a ``test_*``
#: file anywhere, or a ``*_test.py`` file anywhere.
TEST_PATH_RE = re.compile(r"(^|/)tests?/.+\.py$|(^|/)test_[^/]*\.py$|_test\.py$")

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


def _parse_exempt_text(text: str, source: str) -> set[str]:
    """Parse exempt-file ``text`` read from ``source`` (path label for errors).

    40-hex shas only; ``#`` comments and blank lines are ignored.  Raises
    ``ExemptError`` on the first malformed line.
    """
    exempt: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if EXEMPT_SHA_RE.match(line) is None:
            raise ExemptError(f"exempt file {source}:{lineno}: not a 40-hex sha: {line!r}")
        exempt.add(line.lower())
    return exempt


def load_exempt(path: str | Path) -> set[str]:
    """Parse the exempt file on disk: 40-hex shas, ``#`` comments and blanks."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ExemptError(f"cannot read exempt file {path}: {exc}") from exc

    return _parse_exempt_text(text, str(path))


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


def _writer_values(message: str) -> list[str]:
    """Every ``writer:`` line value in ``message``, stripped, in order.

    The list is empty only when no ``writer:`` line exists at all; a line with a
    blank value yields ``""`` (and therefore does not count as a Writer trailer).
    """
    values: list[str] = []
    for line in message.splitlines():
        match = WRITER_RE.match(line)
        if match:
            values.append(match.group(1).strip())
    return values


def _has_test_removal_trailer(message: str) -> bool:
    return any(TEST_REMOVAL_RE.match(line) for line in message.splitlines())


def _removes_test(sha: str, cwd: str | Path | None = None) -> bool:
    """True when the commit deletes a test file or drops a ``def test_`` line.

    Test files are ``tests?/.../*.py``, ``test_*.py`` and ``*_test.py``; the
    removed-line scan walks the whole commit diff and counts a removed
    ``def test_`` line only inside a file matching that pattern.
    """
    deleted = _git_text(["show", "--format=", "--diff-filter=D", "--name-only", sha], cwd=cwd)
    for line in deleted.splitlines():
        if TEST_PATH_RE.search(line.strip()):
            return True

    diff = _git_text(["show", "--format=", "-U0", sha], cwd=cwd)
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("--- ") or line.startswith("+++ "):
            # File header: remember which file the hunk belongs to.  ``/dev/null``
            # keeps the side that named a real path (deleted files say it once).
            path = line[4:].strip()
            if path != "/dev/null":
                current = path[2:] if path[:2] in ("a/", "b/") else path
        elif current and TEST_PATH_RE.search(current) and REMOVED_TEST_DEF_RE.match(line):
            return True
    return False


def _exempt_at_base(base: str, rel: str, cwd: str | Path | None = None) -> set[str] | None:
    """Exempt shas recorded in the ``base`` commit for repo-relative ``rel``.

    ``None`` when the file is absent at ``base`` (a bootstrap PR that introduces
    the exempt file), so the caller falls back to the working tree.
    """
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{base}:{rel}"], cwd=cwd, capture_output=True
    )
    if probe.returncode != 0:
        return None
    text = _git_text(["show", f"{base}:{rel}"], cwd=cwd)
    return _parse_exempt_text(text, f"{base}:{rel}")


def _resolve_exempt(
    base: str, exempt: str | Path, cwd: str | Path | None = None
) -> set[str]:
    """Load ``exempt``, preferring the BASE commit's copy of a repo-relative path.

    A PR must not exempt its own commits: when ``exempt`` is repo-relative and
    exists at ``base``, the base copy wins over the working tree.  A bootstrap PR
    (no file at ``base``) uses the working-tree file, resolved against ``cwd``.
    """
    rel = Path(exempt).as_posix() if not Path(exempt).is_absolute() else None
    if rel is not None:
        at_base = _exempt_at_base(base, rel, cwd=cwd)
        if at_base is not None:
            return at_base
        path: str | Path = Path(cwd) / rel if cwd is not None else exempt
        return load_exempt(path)
    return load_exempt(exempt)


def check(
    base: str,
    head: str,
    exempt: str | Path | None = None,
    cwd: str | Path | None = None,
) -> list[str]:
    """Return one ``sha40: reason`` line per violation in ``base..head``.

    Raises ``ExemptError`` for a bad exempt file and ``GateError`` when git fails.
    """
    exempt_set = _resolve_exempt(base, exempt, cwd=cwd) if exempt else set()

    violations: list[str] = []
    for sha, message in _iter_commits(base, head, cwd=cwd):
        if sha in exempt_set:
            continue

        writers = _writer_values(message)
        if not any(writers):
            violations.append(f"{sha}: {MISSING_WRITER}")
        elif any(marker in value.lower() for value in writers for marker in CLAUDE_MARKERS):
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
