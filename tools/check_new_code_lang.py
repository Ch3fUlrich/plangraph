#!/usr/bin/env python3
"""Compatibility entry point for the Rust new-code gate (D-5912).

PG_POLICY_BIN names a prebuilt pg-policy (tools/preflight.py builds it once, outside
the per-check bound). Committed Class-Search checks also use the source-keyed
cache so concurrent checkouts cannot substitute an older policy binary."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    manifest = Path(__file__).resolve().parents[1] / "rust/Cargo.toml"
    base = os.environ.get("CLASS_SEARCH_BASE")
    binary = os.environ.get("PG_POLICY_BIN")
    if base and not (binary and Path(binary).is_file()):
        from preflight import compile_policy, policy_cached_executable, policy_target
        root = manifest.parent.parent
        target = os.environ.get("CARGO_TARGET_DIR")
        keydir = policy_target(root, Path(target).absolute() if target else None)
        binary = policy_cached_executable(keydir)
        if binary is None:
            code, output, binary = compile_policy(root, os.environ, keydir)
            print(output, end="", flush=True)
            if code or not (binary and Path(binary).is_file()):
                print("pg-policy build did not produce a usable executable", file=sys.stderr)
                return code or 1
    if binary and Path(binary).is_file():
        run = [binary]
    else:
        run = ["cargo", "run", "--quiet", "--locked", "--manifest-path", str(manifest),
               "-p", "pg-policy", "--"]
    guard = subprocess.call([*run, "--test-sources", str(manifest.parent.parent)])
    if guard:
        return guard
    guard = subprocess.call([*run, *sys.argv[1:]])
    if guard:
        return guard
    return subprocess.call([*run, "--class-search", "--range", base]) if base else 0


if __name__ == "__main__":
    raise SystemExit(main())
