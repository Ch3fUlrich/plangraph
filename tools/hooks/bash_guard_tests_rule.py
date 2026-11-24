"""Opt-in placement policy. Command inspection reads no host state, except that the cargo target rule
probes the filesystem with `git rev-parse` (2 s timeout) to find the checkout a cargo build would fill."""
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
_PGTEST = ("pgtest", "pgtest.py")

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


MAIN_TARGET_MESSAGE = ("bash-guard: DENIED - cargo %s in the checkout %s (main or .claude/worktrees) would write its target "
                       "inside it, outside every disk cap; set CARGO_TARGET_DIR (or --target-dir) to the shared target (%s)")
DEFAULT_SHARED_TARGET = "~/.cache/plangraph/cargo-target"


def shared_target():
    """The host's shared target: PLANGRAPH_CARGO_TARGET_DIR (a data drive on some hosts), else the default."""
    return os.environ.get("PLANGRAPH_CARGO_TARGET_DIR") or DEFAULT_SHARED_TARGET


_TARGET_ENVS = ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR")
_BUILDING = frozenset(("build", "b", "check", "c", "test", "t", "nextest", "clippy", "run", "r",
                       "bench", "doc", "d", "rustc", "llvm-cov"))


def _agent_worktree(top):
    """A linked worktree under .claude/worktrees (a writer's checkout) is as uncapped as a main one."""
    return any(a.name == ".claude" and (a / "worktrees") == b for a, b in zip(top.parents, [top, *top.parents]))


def main_checkout_root(cwd):
    """Root of the main checkout, or of a linked worktree under .claude/worktrees, containing cwd; else None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd or Path.cwd()), "rev-parse", "--path-format=absolute",
             "--show-toplevel", "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, check=True, timeout=2).stdout.split("\n")
        top, git_dir, common = (Path(line).resolve() for line in out[:3])
        # A repository at the home directory is a dotfiles store, not a project: the shared target
        # and every cache live below it.
        if top == Path.home().resolve():
            return None
        return top if git_dir == common or _agent_worktree(top) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_CARGO_VALUE_OPTIONS = frozenset(("--color", "-Z", "-C", "--config"))


def _expand(path, base):
    return (Path(base) / Path(os.path.expandvars(path)).expanduser()).resolve()


def _cargo_workdir(rest, cwd):
    """Directory cargo builds in: `-C DIR` (before the subcommand), then `--manifest-path`'s directory."""
    where, it = Path(cwd), iter(rest)
    for word in it:
        if word == "-C":
            where = _expand(next(it, "."), where)
        elif word in _CARGO_VALUE_OPTIONS:
            next(it, None)
        elif not word.startswith(("+", "-")):
            break
    for k, word in enumerate(rest):
        manifest = (rest[k + 1] if word == "--manifest-path" and k + 1 < len(rest)
                    else word.split("=", 1)[1] if word.startswith("--manifest-path=") else None)
        if manifest:
            return _expand(manifest, where).parent
    return where


def _nearest_checkout(path):
    """Main checkout or agent worktree root holding `path`, found from its nearest existing ancestor."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return main_checkout_root(candidate)
    return None


def _cargo_target_checkout(rest, envs, where, host_env=None):
    """The one target resolver: the main checkout or agent worktree a cargo build would fill, else None.

    The target is `--target-dir`, then `CARGO_TARGET_DIR`/`CARGO_BUILD_TARGET_DIR`, relative to the
    directory cargo runs in (cwd, `cd`, `-C`, `--manifest-path`); with none, cargo writes
    `<workspace>/target` inside the checkout that directory belongs to.
    """
    target = next((rest[k + 1] for k, a in enumerate(rest[:-1]) if a == "--target-dir"), None)
    target = target or next((a.split("=", 1)[1] for a in rest if a.startswith("--target-dir=")), None)
    host_env = os.environ if host_env is None else host_env
    for var in _TARGET_ENVS:
        # A name mapped to None was unset (`env -u`, `unset`): it must not fall back to the hook's own environment.
        value = envs[var] if var in envs else host_env.get(var)
        target = target or value or None
    if target is None:
        return main_checkout_root(where)
    try:
        return _nearest_checkout(_expand(target, where))
    except (OSError, RuntimeError, ValueError):
        return None


def _cargo_subcommand(rest):
    """First word of cargo's arguments that is not a +toolchain or a global option."""
    it = iter(rest)
    for word in it:
        if word in _CARGO_VALUE_OPTIONS:
            next(it, None)
        elif not word.startswith(("+", "-")):
            return word
    return None


def _env_options(words):
    """What `env`'s options before the command do to the environment: names unset, and whether it is cleared."""
    unset, cleared, it = set(), False, iter(words)
    for word in it:
        if word in ("-i", "--ignore-environment", "-"):
            cleared = True
        elif word in ("-u", "--unset"):
            unset.add(next(it, ""))
        elif word.startswith("--unset="):
            unset.add(word.split("=", 1)[1])
    return unset, cleared


def _record_export(state, builtin, args):
    """What `export` / `declare` / `typeset` do to the variables later commands see.

    `export NAME=VAL` and `declare -x NAME=VAL` export (`NAME+=VAL` appends); `export -n` and `declare +x`
    stop exporting (the name maps to None). `declare` without -x only sets a shell variable.
    """
    flags = "".join(w[1:] for w in args if re.match(r"^-[A-Za-z]+$", w))
    unflags = "".join(w[1:] for w in args if re.match(r"^\+[A-Za-z]+$", w))
    unexport = "n" in flags if builtin == "export" else "x" in unflags
    exporting = not unexport and (builtin == "export" or "x" in flags)
    for word in args:
        name, has_value, value = word.partition("=")
        if name.endswith("+") and has_value:
            name = name[:-1]
            previous = state[name] if name in state else os.environ.get(name)
            value = (previous or "") + value
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        if unexport:
            state[name] = None
        elif exporting and has_value:
            state[name] = value


def main_checkout_cargo(command, cwd, depth=0, inherited=None):
    """Deny message for a cargo build whose target dir is a main checkout's or agent worktree's own, wherever cwd, `cd`, `-C` or `--manifest-path` put it."""
    if depth > 3:
        return None
    from bash_guard_rules import _ENV_ASSIGN_RE, _SHELLS, _WRAPPERS, _basename, _shell_strings
    from bash_guard_rules2 import lead, scan
    cwd = Path(cwd or Path.cwd())
    commands, _ = scan(command)
    state = dict(inherited or {})  # variables later commands see, with `unset` names mapped to None
    for cmd in commands:
        pos = [w for w in cmd if not w.redir]
        words = [w.text for w in pos]
        i = lead(pos)
        envs = dict(state)
        start = i
        while i < len(words) and _ENV_ASSIGN_RE.match(words[i]):
            i += 1
        envs.update(w.split("=", 1) for w in words[start:i])  # only the assignments before the command word
        if i >= len(words):
            continue
        if _basename(words[i]) == "unset":
            state.update({name: None for name in words[i + 1:] if not name.startswith("-")})
            continue
        if _basename(words[i]) in ("export", "declare", "typeset"):
            _record_export(state, _basename(words[i]), words[i + 1:])
            continue
        # Wrapper options are not modelled: every later word may be the command, as check_command reads it.
        wrapped = _basename(words[i]) in _WRAPPERS | _PLACEMENT_WRAPPERS or any(_basename(w) in _PGTEST for w in words[i:i + 3])
        evaled = False
        if _basename(words[i]) in ("cd", "pushd") and len(words) > i + 1:
            cwd = _expand(words[i + 1], cwd)  # later commands run there
            continue
        for j in range(i, len(words)) if wrapped else (i,):
            name = _basename(words[j])
            if name in _SHELLS:
                nested = _shell_strings(pos, j + 1)
            elif name == "eval" and not evaled:
                evaled, nested = True, [" ".join(words[j + 1:])]
            else:
                nested = []
            given = dict(w.split("=", 1) for w in words[i:j] if _ENV_ASSIGN_RE.match(w))  # `env NAME=VAL` before this word
            for string in nested:
                reason = main_checkout_cargo(string, cwd, depth + 1, {**envs, **given})
                if reason:
                    return reason
            if name != "cargo":
                continue
            rest = words[j + 1:]
            sub = _cargo_subcommand(rest)
            if sub not in _BUILDING:
                continue
            unset, cleared = _env_options(words[i:j])
            if cleared:  # `env -i`: only the assignments given to env itself survive
                run_env = dict(given)
            else:
                run_env = {**envs, **given}
            run_env.update({name: None for name in unset})
            root = _cargo_target_checkout(rest, run_env, _cargo_workdir(rest, cwd), {} if cleared else None)
            if root is not None:
                return MAIN_TARGET_MESSAGE % (sub, root, shared_target())
    return None
