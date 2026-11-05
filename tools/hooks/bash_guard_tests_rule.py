"""Opt-in placement policy; command inspection itself has no host I/O."""
import os
import re
from pathlib import Path

MESSAGE = "bare test run on this host: use pgtest <command> (runs in agent-tests.slice)"

# Only separate option operands need skipping; --option=value is one word.
_VALUE_OPTIONS = {
    "env": "-u --unset -C --chdir -S --split-string --argv0",
    "time": "-f --format -o --output",
    "nice": "-n --adjustment",
    "sudo": "-u --user -g --group -h --host -p --prompt -C --close-from "
            "-T --command-timeout -R --chroot -D --chdir -r --role -t --type",
    "timeout": "-s --signal -k --kill-after",
    "stdbuf": "-i --input -o --output -e --error",
    "exec": "-a",
    "ionice": "-c --class -n --classdata -p --pid -P --pgid -u --uid",
    "systemd-run": "--unit --description --slice -p --property --working-directory "
                   "--service-type --uid --gid --nice -E --setenv "
                   "--on-active --on-boot --on-startup --on-unit-active "
                   "--on-unit-inactive --on-calendar --on-clock-change "
                   "--on-timezone-change --timer-property --path-property "
                   "--socket-property -H --host -M --machine --job-mode",
    "xargs": "-a --arg-file -d --delimiter -E -I --replace -L --max-lines "
             "-n --max-args -P --max-procs -s --max-chars --process-slot-var",
}


def tests_required():
    """Unreadable or missing marker files do not opt a host in."""
    try:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        with (root / "plangraph" / "pgtest-required").open("rb"):
            return True
    except (OSError, ValueError, RuntimeError):
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
                j = 0
                while j < len(args):
                    arg = args[j]
                    if arg == "-m":
                        return (check_bare_tests(pos_words[i + j + 2:], False)
                                if args[j + 1:j + 2] == ["pytest"] else None)
                    if arg in ("-c", "--") or not arg.startswith("-"):
                        break
                    j += 2 if arg in ("-W", "-X", "--check-hash-based-pycs") else 1
            if name == "cargo" and args and args[0].startswith("+"):
                args = args[1:]
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
            takes_value = arg in _VALUE_OPTIONS.get(wrapper, "").split()
            i += 2 if takes_value else 1
    return None
