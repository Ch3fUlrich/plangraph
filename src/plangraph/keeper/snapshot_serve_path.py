"""LE-SNAPSHOT-SERVE-PATH (D-3445): pin the snapshot-pull forced command's checkout.

The keeper host's ``authorized_keys`` forced command for snapshot pulls pointed its
``PYTHONPATH`` at a ``.claude/worktrees/...`` checkout; a session cleanup removed
that directory and pulls broke SILENTLY.  The forced command must run from the
PINNED checkout ``~/.local/share/plangraph/snapshot-serve`` (``PINNED_DEFAULT``) —
outside any ``.claude/worktrees/`` dir — which is REFRESHED ONLY BY THE LIVE-SYNC
STEP: never by agents, never by hand.

sshd runs the ``command=`` value through ``sh -c``, so ``forced_command`` shell-quotes
every path and ``check_authorized_keys`` re-parses the value strictly (``shlex``):
any snapshot-serve line whose PYTHONPATH it cannot read is a PROBLEM, never a skip.
Problems name only the line number and the PYTHONPATH — never key material.
Every PYTHONPATH entry must REALPATH to ``<pinned>/src`` — the pin is enforced,
not advisory — and must be outside ``.claude/worktrees/`` by REALPATH as well as
lexically.  ``print`` refuses a ``--checkout`` under ``.claude/worktrees/``, a
relative path, or one that is not the pinned checkout.

Installing or changing the forced command is OPERATOR STEP OS-SNAPSHOT-PULL-PATH;
agents never edit ``authorized_keys``.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys

__all__ = ["PINNED_DEFAULT", "forced_command", "check_authorized_keys", "main"]

#: The snapshot-serve checkout outside any ``.claude/worktrees/`` dir.  Refreshed
#: only by the live-sync step; session cleanup may never remove it.
PINNED_DEFAULT = "~/.local/share/plangraph/snapshot-serve"

_SNIPPET = "plangraph.keeper.snapshot_serve"
#: A command value is inspected when it contains this substring: covers both
#: ``-m plangraph.keeper.snapshot_serve`` and a file-path ``.../snapshot_serve.py``.
_TRIGGER = "snapshot_serve"
#: ``command="..."`` value, honoring ``\\"`` escapes, at an option boundary only.
_COMMAND_RE = re.compile(r'(?:^|,)command="((?:\\.|[^"\\])*)"')
#: Unquoted ``command=`` value (up to the next option comma), quotes included.
_COMMAND_BARE_RE = re.compile(r"(?:^|,)command=([^,]*)")
_UNESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def _abs(path) -> str:
    """Absolute form of ``path``.

    ``~`` and relative paths resolve against the CURRENT process's HOME/cwd: right
    when generating (``forced_command`` runs on the keeper host), wrong for a
    checker, so ``check_authorized_keys`` requires absolute PYTHONPATH entries and
    reports ``not absolute`` for anything ``~``- or cwd-relative.
    """
    return os.path.abspath(os.path.expanduser(str(path)))


def forced_command(checkout, config) -> str:
    """The exact ``command=`` option text for the snapshot-pull authorized_keys line.

    Both paths are shell-quoted (``shlex.quote``) and the command is escaped for the
    authorized_keys ``"..."`` option value, so a space or quote in a path cannot
    break the line — nor the re-parse in ``check_authorized_keys``.
    """
    words = ["PYTHONPATH=" + shlex.quote(_abs(checkout) + "/src"),
             "/usr/bin/python3", "-m", _SNIPPET,
             "--config", shlex.quote(_abs(config))]
    inner = " ".join(words).replace("\\", "\\\\").replace('"', '\\"')
    return 'command="' + inner + '"'


def _under_claude_worktrees(path: str) -> bool:
    parts = os.path.normpath(path).split(os.sep)
    return any(a == ".claude" and b == "worktrees" for a, b in zip(parts, parts[1:]))


def _options_field(line: str) -> str:
    """The authorized_keys options field: everything before the key type/key.

    Stops at the first whitespace outside a quoted value, so a ``command="..."``
    sitting in the comment field is never mistaken for an option.
    """
    out, quote, i = [], "", 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i:i + 2])
                i += 2
                continue
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch.isspace():
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _command_value(options: str):
    """The forced command's raw value text, or None if no ``command=`` option.

    ``_COMMAND_RE`` handles escaped ``\\"`` inside ``command="..."`` and only scans
    the options field; an unquoted ``command=`` value is returned as written.
    """
    match = _COMMAND_RE.search(options)
    if match:
        return _UNESCAPE_RE.sub(r"\1", match.group(1))
    match = _COMMAND_BARE_RE.search(options)
    return match.group(1) if match else None


def _check_command(number: int, command: str, pinned) -> list:
    try:
        words = shlex.split(command)
    except ValueError:
        return [f"line {number}: snapshot_serve command cannot be parsed"]
    entries = []
    for word in words:
        if word.startswith("PYTHONPATH="):
            # keep empty entries: CPython maps them to the cwd
            entries += word.split("=", 1)[1].split(os.pathsep)
    if not entries:
        return [f"line {number}: no PYTHONPATH in snapshot_serve command"]
    pinned_dir = os.path.expanduser(str(pinned))
    pinned_src = os.path.realpath(os.path.join(pinned_dir, "src"))
    problems = []
    for entry in entries:
        if not entry:
            problems.append(f"line {number}: PYTHONPATH has an empty entry "
                            "(CPython maps it to the cwd)")
            continue
        if not os.path.isabs(entry):
            problems.append(f"line {number}: PYTHONPATH {entry} is not absolute")
        elif not os.path.isdir(entry):
            problems.append(f"line {number}: PYTHONPATH {entry} does not exist")
        if _under_claude_worktrees(entry) or _under_claude_worktrees(os.path.realpath(entry)):
            problems.append(f"line {number}: PYTHONPATH {entry} is under "
                            ".claude/worktrees/ (removable by session cleanup)")
        if os.path.realpath(entry) != pinned_src:
            problems.append(f"line {number}: PYTHONPATH {entry} is not the pinned "
                            f"checkout {pinned_dir}/src")
    return problems


def check_authorized_keys(text: str, pinned=PINNED_DEFAULT) -> list:
    """Problems in ``text``; only snapshot-serve lines are inspected.

    For every ``command=`` whose value runs ``snapshot_serve`` (module or file
    path spelling): every PYTHONPATH entry (all ``PYTHONPATH=`` words, all
    ``os.pathsep`` dirs) must be an EXISTING absolute directory outside
    ``.claude/worktrees/`` (by realpath as well as lexically) whose realpath is
    exactly ``realpath(pinned/src)`` — the pin is ENFORCED.  An empty entry, or a
    line whose command cannot be parsed or carries no parseable PYTHONPATH, is
    itself a problem — never a skip.  Each problem names only the line number
    and the path.
    """
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        command = _command_value(_options_field(stripped))
        if command is None or _TRIGGER not in command:
            continue
        problems += _check_command(number, command, pinned)
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot_serve_path")
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("print", help="print the forced command to install")
    show.add_argument("--checkout", required=True)
    show.add_argument("--config", required=True)
    show.add_argument("--pinned", default=PINNED_DEFAULT)
    check = sub.add_parser("check", help="check an authorized_keys file (exit 1 on problems)")
    check.add_argument("--authorized-keys", required=True)
    check.add_argument("--pinned", default=PINNED_DEFAULT)
    args = parser.parse_args(argv)
    if args.cmd == "print":
        checkout = os.path.expanduser(args.checkout)
        if (_under_claude_worktrees(checkout)
                or _under_claude_worktrees(os.path.realpath(checkout))):
            print("snapshot_serve_path: refusing --checkout under .claude/worktrees/ "
                  "(removable by session cleanup)", file=sys.stderr)
            return 1
        if not os.path.isabs(checkout):
            print("snapshot_serve_path: refusing relative --checkout", file=sys.stderr)
            return 1
        pinned = os.path.expanduser(str(args.pinned))
        if os.path.realpath(checkout) != os.path.realpath(pinned):
            print(f"snapshot_serve_path: refusing --checkout {checkout} is not the "
                  f"pinned checkout {pinned}", file=sys.stderr)
            return 1
        print(forced_command(args.checkout, args.config))
        return 0
    try:
        with open(os.path.expanduser(args.authorized_keys), encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"snapshot_serve_path: {exc}", file=sys.stderr)
        return 2
    problems = check_authorized_keys(text, pinned=args.pinned)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
