"""Opt-in placement policy; command inspection itself has no host I/O."""
import os
import re
from pathlib import Path

MESSAGE = "bare test run on this host: use pgtest <command> (runs in agent-tests.slice)"


def tests_required():
    """Unreadable or missing marker files do not opt a host in."""
    try:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        with (root / "plangraph" / "pgtest-required").open("rb"):
            return True
    except (OSError, ValueError):
        return False


def check_bare_tests(pos_words, wrapped_in_slice) -> str | None:
    """Inspect command positions, never arbitrary program arguments.

    Wrapper options with separate operands are skipped before inspecting the
    wrapped command. Import wrapper metadata lazily to avoid a module cycle.
    """
    from bash_guard_rules import _WRAPPERS, _ENV_ASSIGN_RE, _basename

    words = [w.text for w in pos_words]
    i = 0
    while i < len(words):
        word = words[i]
        name = _basename(word)
        if _ENV_ASSIGN_RE.match(word):
            i += 1
            continue
        if name == "pgtest" or wrapped_in_slice:
            return None
        if name not in _WRAPPERS:
            args = words[i + 1:]
            if name in ("pytest", "py.test"):
                return None if any(a in ("--version", "--collect-only") for a in args) else MESSAGE
            if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name):
                for j, arg in enumerate(args[:-1]):
                    if arg == "-m" and args[j + 1] == "pytest":
                        return check_bare_tests(pos_words[i + j + 2:], False)
                    if not arg.startswith("-"):
                        break
            if name == "cargo" and args and args[0] in ("test", "nextest"):
                return MESSAGE
            return None
        wrapper = name
        i += 1
        while i < len(words):
            arg = words[i]
            if arg == "--":
                i += 1
                break
            if _ENV_ASSIGN_RE.match(arg):
                i += 1
                continue
            if not arg.startswith("-"):
                if wrapper == "timeout":
                    i += 1  # duration
                break
            if wrapper == "systemd-run" and (arg == "--slice=agent-tests.slice" or
                    (arg == "--slice" and words[i + 1:i + 2] == ["agent-tests.slice"])):
                return None
            takes_value = arg in ("-n", "-u", "-g", "-p", "--slice", "--property",
                                  "-s", "-k", "--signal", "--kill-after", "-C")
            i += 2 if takes_value else 1
    return None
