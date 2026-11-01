"""LE-SNAPSHOT-SERVE-PATH (D-3445): pin the snapshot-pull forced command's checkout.

The keeper host's ``authorized_keys`` forced command for snapshot pulls pointed its
``PYTHONPATH`` at a ``.claude/worktrees/...`` checkout; a session cleanup removed
that directory and pulls broke SILENTLY.  The forced command must run from the
PINNED checkout ``~/.local/share/plangraph/snapshot-serve`` (``PINNED_DEFAULT``) —
outside any ``.claude/worktrees/`` dir — which is REFRESHED ONLY BY THE LIVE-SYNC
STEP: never by agents, never by hand.  This module prints the exact ``command=``
text to install and checks an ``authorized_keys`` file for broken or
cleanup-removable PYTHONPATH dirs.  It never prints key material: a problem names
only the line number and the PYTHONPATH.

Installing or changing the forced command is OPERATOR STEP OS-SNAPSHOT-PULL-PATH;
agents never edit ``authorized_keys``.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

__all__ = ["PINNED_DEFAULT", "forced_command", "check_authorized_keys", "main"]

#: The snapshot-serve checkout outside any ``.claude/worktrees/`` dir.  Refreshed
#: only by the live-sync step; session cleanup may never remove it.
PINNED_DEFAULT = "~/.local/share/plangraph/snapshot-serve"

_SNIPPET = "plangraph.keeper.snapshot_serve"
_COMMAND_RE = re.compile(r'command="([^"]*)"')
_PYPATH_RE = re.compile(r'(?:^|\s)PYTHONPATH=([^\s"]+)')


def _abs(path) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def forced_command(checkout, config) -> str:
    """The exact ``command=`` option text for the snapshot-pull authorized_keys line."""
    return ('command="PYTHONPATH={}/src /usr/bin/python3 -m {} --config {}"'
            .format(_abs(checkout), _SNIPPET, _abs(config)))


def _under_claude_worktrees(path: str) -> bool:
    parts = os.path.normpath(path).split(os.sep)
    return any(a == ".claude" and b == "worktrees" for a, b in zip(parts, parts[1:]))


def check_authorized_keys(text: str) -> list:
    """Problems in ``text``; only snapshot-serve lines are inspected.

    For every ``command=`` containing ``plangraph.keeper.snapshot_serve``: (a) each
    PYTHONPATH dir that does not exist, (b) each PYTHONPATH under a
    ``.claude/worktrees/`` path (removable by session cleanup).  Each problem names
    only the line number and the PYTHONPATH — never key material.
    """
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        command = _COMMAND_RE.search(line)
        if not command or _SNIPPET not in command.group(1):
            continue
        pypath = _PYPATH_RE.search(command.group(1))
        if not pypath:
            continue
        for entry in pypath.group(1).split(os.pathsep):
            if not entry:
                continue
            dir_path = _abs(entry)
            if not os.path.isdir(dir_path):
                problems.append(f"line {number}: PYTHONPATH {dir_path} does not exist")
            if _under_claude_worktrees(dir_path):
                problems.append(f"line {number}: PYTHONPATH {dir_path} is under "
                                ".claude/worktrees/ (removable by session cleanup)")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot_serve_path")
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("print", help="print the forced command to install")
    show.add_argument("--checkout", required=True)
    show.add_argument("--config", required=True)
    check = sub.add_parser("check", help="check an authorized_keys file (exit 1 on problems)")
    check.add_argument("--authorized-keys", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "print":
        print(forced_command(args.checkout, args.config))
        return 0
    try:
        with open(os.path.expanduser(args.authorized_keys), encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"snapshot_serve_path: {exc}", file=sys.stderr)
        return 2
    problems = check_authorized_keys(text)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
