#!/usr/bin/env python3
"""Render a probed operator step without executing its commands (stdlib only)."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from plangraph.keeper.oversight_operator import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("render")
    command.add_argument("--host", required=True)
    command.add_argument("--tested", required=True)
    command.add_argument("--shell", choices=("sh", "pwsh"), default="sh")
    command.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        print(render(args.host, args.tested, args.file.read_text(), args.shell), end="")
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
