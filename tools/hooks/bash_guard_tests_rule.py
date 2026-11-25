"""Opt-in placement policy; command inspection itself has no host I/O."""
import os
import re
import subprocess
from pathlib import Path

TRAIN_MESSAGE = ("full-tree pytest is disabled on land/* and lane/* branches; "
                 "use python3 tools/affected_tests.py --run or supply an operator D-id "
                 "in PLANGRAPH_FULL_PYTEST_D_ID")
_PYTEST_VALUE_OPTIONS = frozenset((
    "-k", "-m", "-n", "-o", "-p", "-c", "--numprocesses", "--dist", "--override-ini",
    "--maxfail", "--tb", "--capture", "--basetemp", "--rootdir", "--confcutdir",
    "--ignore", "--ignore-glob", "--deselect", "--durations", "--durations-min",
    "--junitxml", "--junit-xml", "--log-file", "--log-level", "--color", "--import-mode",
))

MESSAGE = "bare test run on this host: use pgtest <command> (runs in agent-tests.slice)"
_PLACEMENT_WRAPPERS = frozenset(("sudo", "time", "xargs", "stdbuf", "ionice", "exec"))

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


def train_checkout(cwd):
    """Resolve the train branch and root of the worktree the tool will use."""
    if re.fullmatch(r"D-[0-9]+", os.environ.get("PLANGRAPH_FULL_PYTEST_D_ID", "")):
        return None
    try:
        cwd = Path(cwd or Path.cwd()).resolve()

        def git(*args):
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                                  text=True, check=True, timeout=2).stdout.rstrip("\n")

        branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.startswith(("land/", "lane/")):
            return cwd, Path(git("rev-parse", "--show-toplevel"))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def full_tree_pytest(args, checkout):
    """Look for a checkout-wide selector or pytest's implicit selection."""
    cwd, root = checkout
    suite = (root / "tests").resolve()
    narrowed, positional = False, False
    arguments = iter(args)
    for arg in arguments:
        if arg == "--" and not positional:
            positional = True
        elif not positional and arg.startswith("-"):
            if arg in _PYTEST_VALUE_OPTIONS:
                next(arguments, None)
        else:
            path = (cwd / arg.split("::", 1)[0]).resolve()
            if path == suite or path in suite.parents:
                return True
            narrowed |= path.is_relative_to(root) and path.exists()
    return not narrowed


def check_bare_tests(pos_words, wrapped_in_slice, *, placement_required=True,
                     checkout=None) -> str | None:
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
        if name in ("pgtest", "pgtest.py"):
            if checkout is None:
                return None
            wrapped_in_slice = True
            i += 1
            continue
        if wrapped_in_slice and checkout is None:
            return None
        if name not in _WRAPPERS | _PLACEMENT_WRAPPERS:
            args = words[i + 1:]
            if name in ("pytest", "py.test"):
                if any(a in ("--version", "--collect-only", "-h", "--help") for a in args):
                    return None
                if checkout and full_tree_pytest(args, checkout):
                    return TRAIN_MESSAGE
                return MESSAGE if placement_required and not wrapped_in_slice else None
            if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name):
                j = 0
                while j < len(args):
                    arg = args[j]
                    if arg == "-m":
                        return (check_bare_tests(pos_words[i + j + 2:], wrapped_in_slice,
                                                 placement_required=placement_required, checkout=checkout)
                                if args[j + 1:j + 2] in (["pytest"], ["py.test"]) else None)
                    if checkout and _basename(arg) == "pgtest.py":
                        return check_bare_tests(pos_words[i + j + 1:], wrapped_in_slice,
                                                placement_required=placement_required, checkout=checkout)
                    if arg in ("-c", "--") or not arg.startswith("-"):
                        break
                    j += 2 if arg in ("-W", "-X", "--check-hash-based-pycs") else 1
            if name == "cargo" and args and args[0].startswith("+"):
                args = args[1:]
            if placement_required and not wrapped_in_slice and name == "cargo" and args and args[0] in ("test", "nextest"):
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
                wrapped_in_slice = True
            takes_value = arg in _VALUE_OPTIONS.get(wrapper, "").split()
            i += 2 if takes_value else 1
    return None
