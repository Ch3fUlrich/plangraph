#!/usr/bin/env python3
"""D-4691: new production code is Rust; existing code may still be edited."""
import argparse
import fnmatch
import os
from pathlib import Path
import subprocess

ALLOWLIST = "tools/new_code_allowlist.txt"
BLOCKED = {".py", ".sh", ".js", ".ts", ".mjs", ".cjs", ".tsx"}


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


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
    changes = git("diff", "--find-renames", "--diff-filter=A", "--name-only",
                  "-z", f"{resolved.stdout.strip()}...HEAD", "--")
    if changes.returncode:
        print(f"check-new-code-lang: git diff failed: {changes.stderr.strip()}")
        return 1
    for path in changes.stdout.split("\0"):
        if (Path(path).suffix in BLOCKED
                and not any(fnmatch.fnmatchcase(path, glob) for glob in allowed)):
            errors.append(f"FAIL {path}: new code must be Rust (D-4691)")
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
