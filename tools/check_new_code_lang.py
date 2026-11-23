#!/usr/bin/env python3
"""Compatibility entry point for the Rust new-code gate (D-5912).

PG_POLICY_BIN names a prebuilt pg-policy (tools/preflight.py builds it once, outside
the per-check bound); without it the gate compiles through cargo run."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    manifest = Path(__file__).resolve().parents[1] / "rust/Cargo.toml"
    binary = os.environ.get("PG_POLICY_BIN")
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
    base = os.environ.get("CLASS_SEARCH_BASE")
    return subprocess.call([*run, "--class-search", "--range", base]) if base else 0


if __name__ == "__main__":
    raise SystemExit(main())
