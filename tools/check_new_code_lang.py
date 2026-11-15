#!/usr/bin/env python3
"""Compatibility entry point for the Rust new-code gate (D-5912)."""
from pathlib import Path
import subprocess
import sys


def main():
    manifest = Path(__file__).resolve().parents[1] / "rust/Cargo.toml"
    return subprocess.call([
        "cargo", "run", "--quiet", "--locked", "--manifest-path", str(manifest),
        "-p", "pg-policy", "--", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
