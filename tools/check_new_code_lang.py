#!/usr/bin/env python3
"""D-4691: new production code is Rust; existing code may still be edited."""
import argparse
import fnmatch
import os
from pathlib import Path
import re
import subprocess

ALLOWLIST = "tools/new_code_allowlist.txt"
BLOCKED = {".py", ".sh", ".bash", ".js", ".jsx", ".ts", ".mts", ".cts",
           ".mjs", ".cjs", ".tsx"}


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def matches(path, pattern):
    """Match globs by segment; only a whole ** segment crosses directories."""
    parts, globs = path.split("/"), pattern.split("/")

    def match(i, j):
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return (i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j])
                and match(i + 1, j + 1))

    return match(0, 0)


def blocked(path, revision):
    if Path(path).suffix.lower() in BLOCKED:
        return True
    # Read only the prefix, never materializing an arbitrarily large blob.
    try:
        with subprocess.Popen(["git", "show", f"{revision}:{path}"],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            prefix = proc.stdout.read(256)
            proc.stdout.close()
            proc.terminate()
        if b"\0" in prefix:
            return False
        line = prefix.decode("utf-8").split("\n", 1)[0]
    except (OSError, UnicodeError):
        return False
    return line.startswith("#!") and bool(re.search(
        r"(?:^|[/\s])(?:python(?:\d+(?:\.\d+)*)?|sh|bash|node)(?=\s|$)",
        line[2:]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="default: $NEWCODE_BASE or origin/main")
    args = parser.parse_args()
    root = git("rev-parse", "--show-toplevel")
    if root.returncode:
        print("check-new-code-lang: run inside a git checkout")
        return 1
    os.chdir(root.stdout.strip())
    base = args.base or os.environ.get("NEWCODE_BASE") or "origin/main"
    resolved = git("rev-parse", "--verify", "--quiet", base + "^{commit}")
    if resolved.returncode:
        print(f"check-new-code-lang: base {base} does not resolve; fetch the base")
        return 1
    if resolved.stdout.strip() == git("rev-parse", "HEAD").stdout.strip():
        print(f"check-new-code-lang: base {base} is HEAD itself")
        return 1
    allowed, errors = [], []
    try:
        text = Path(ALLOWLIST).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check-new-code-lang: {exc}")
        return 1
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, separator, reason = line.partition("|")
        if not separator or not pattern.strip() or not reason.strip():
            errors.append(f"{ALLOWLIST}:{number}: entry needs a glob | reason")
        else:
            allowed.append(pattern.strip())
    changes = git("diff", "--find-renames", "--find-copies", "--find-copies-harder",
                  "--diff-filter=ARC", "--name-status",
                  "-z", f"{resolved.stdout.strip()}...HEAD", "--")
    if changes.returncode:
        print(f"check-new-code-lang: git diff failed: {changes.stderr.strip()}")
        return 1
    fields = iter(changes.stdout.rstrip("\0").split("\0"))
    old_revision = git("merge-base", resolved.stdout.strip(), "HEAD").stdout.strip()
    for status in fields:
        if not status:
            continue
        old = next(fields)
        path = next(fields) if status[0] in "RC" else old
        if status[0] == "R" and blocked(old, old_revision):
            continue
        if (blocked(path, "HEAD")
                and not any(matches(path, glob) for glob in allowed)):
            errors.append(f"FAIL {path}: new code must be Rust (D-4691); "
                          f"blocked suffixes: {', '.join(sorted(BLOCKED))}; "
                          "Python/shell/Node shebangs are also blocked")
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
