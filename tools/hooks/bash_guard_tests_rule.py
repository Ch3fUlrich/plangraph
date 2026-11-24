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
_TRACKED = _TARGET_ENVS + ("CARGO_HOME",)  # the variables that decide where cargo builds
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
    """`(cwd, project)`: cargo's working directory after `-C DIR` (relative target settings resolve against it)
    and the directory of `--manifest-path` (the default `target` lies in its workspace)."""
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
            return where, _expand(manifest, where).parent
    return where, where


def _nearest_checkout(path):
    """Main checkout or agent worktree root holding `path`, found from its nearest existing ancestor."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return main_checkout_root(candidate)
    return None


def _is_toml(text):
    """True when `text` is valid TOML (a `--config KEY=VALUE`), False for a path."""
    try:
        import tomllib
        tomllib.loads(text)
    except ImportError:
        return "=" in text
    except ValueError:
        return False
    return True


def _toml_target(text):
    """`build.target-dir` of TOML text (a file, or the `build.target-dir=VALUE` of `--config`), else None."""
    try:
        import tomllib
        value = tomllib.loads(text)["build"]["target-dir"]
    except (ImportError, ValueError, KeyError, TypeError):  # no TOML reader, not TOML, or no such key
        return None
    return value if isinstance(value, str) else None


def _configured_target(rest, cwd, envs, host_env):
    """`build.target-dir` as cargo reads it: (path, directory it is relative to), else (None, cwd).

    Cargo's order: `--config` on the command line (the last one that sets it; `KEY=VALUE`, relative to cwd, or a
    config FILE, relative to its directory's parent), then `CARGO_BUILD_TARGET_DIR`, then the files from cwd
    upward (the nearest wins; `.cargo/config` is used when it exists beside a `config.toml`) and
    `$CARGO_HOME/config.toml`, relative to the parent of the directory that holds the file.
    """
    found = (None, cwd)
    for k, word in enumerate(rest):
        given = (rest[k + 1] if word == "--config" and k + 1 < len(rest)
                 else word.split("=", 1)[1] if word.startswith("--config=") else None)
        if given is None:
            continue
        if _is_toml(given):  # `KEY=VALUE`, in any spelling of the key (`build . target-dir`, `"build"."target-dir"`)
            if (value := _toml_target(given)) is not None:
                found = (value, cwd)
            continue
        try:  # otherwise a config file, relative to its directory's parent
            file = _expand(given, cwd)
            if (value := _toml_target(file.read_text())) is not None:
                found = (value, file.parent.parent)
        except OSError:
            pass
    if found[0] is not None:
        return found
    value = envs["CARGO_BUILD_TARGET_DIR"] if "CARGO_BUILD_TARGET_DIR" in envs else host_env.get("CARGO_BUILD_TARGET_DIR")
    if value:
        return value, cwd
    cargo_home = (envs["CARGO_HOME"] if "CARGO_HOME" in envs else host_env.get("CARGO_HOME")) or str(Path.home() / ".cargo")
    directories = [Path(d) / ".cargo" for d in (cwd, *Path(cwd).parents)] + [_expand(cargo_home, cwd)]
    for directory in directories:
        for name in ("config", "config.toml"):
            try:
                value = _toml_target((directory / name).read_text())
            except OSError:
                continue
            if value is not None:
                return value, directory.parent
            break  # the file cargo reads in this directory has no target-dir
    return None, cwd


def _cargo_target_checkout(rest, envs, workdir, host_env=None):
    """The one target resolver: the main checkout or agent worktree a cargo build would fill, else None.

    The target is `--target-dir`, `CARGO_TARGET_DIR`, then cargo's configuration (`_configured_target`), relative
    to the directory cargo runs in (cwd, `cd`, `-C`; never `--manifest-path`); with none, cargo writes
    `<workspace>/target` inside the checkout the manifest's directory belongs to.
    """
    where, project = workdir
    host_env = os.environ if host_env is None else host_env
    target = next((rest[k + 1] for k, a in enumerate(rest[:-1]) if a == "--target-dir"), None)
    target = target or next((a.split("=", 1)[1] for a in rest if a.startswith("--target-dir=")), None)
    # A name mapped to None was unset (`env -u`, `unset`): it must not fall back to the hook's own environment.
    target = target or (envs["CARGO_TARGET_DIR"] if "CARGO_TARGET_DIR" in envs else host_env.get("CARGO_TARGET_DIR")) or None
    if target is None:
        target, where = _configured_target(rest, where, envs, host_env)
    if target is None:
        return main_checkout_root(project)
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


def _env_apply(words, base, cwd):
    """What the `env` wrappers among `words` (the words before the command) do, in order, to the environment `base`
    and the directory `cwd`.

    Returns (environment, cleared, reached, cwd): names mapped to None are unset; `-i` clears everything before
    it; `-C DIR` moves the command; `reached` is False when an `env` would run something else than the next
    word (an option after an assignment: GNU env parses options, then NAME=VALUE operands, then the command).
    """
    from bash_guard_rules import _ENV_ASSIGN_RE, _basename
    env, cleared, n = dict(base), False, 0
    while n < len(words):
        n += 1
        if _basename(words[n - 1]) != "env":
            continue
        while n < len(words) and words[n].startswith("-") and words[n] != "--":
            word, n = words[n], n + 1
            if word == "-" or word == "--ignore-environment":
                env, cleared = {}, True
            elif word in ("--unset", "--chdir", "--split-string"):
                arg, n = (words[n] if n < len(words) else ""), n + 1
                if word == "--unset":
                    env[arg] = None
                elif word == "--chdir":
                    cwd = _expand(arg, cwd)
            elif word.startswith(("--unset=", "--chdir=")):
                name, arg = word.split("=", 1)
                if name == "--unset":
                    env[arg] = None
                else:
                    cwd = _expand(arg, cwd)
            elif not word.startswith("--"):
                for pos, letter in enumerate(word[1:], 1):
                    if letter == "i":
                        env, cleared = {}, True
                    elif letter in "uCS":  # takes the rest of the word, else the next word
                        arg = word[pos + 1:]
                        if not arg:
                            arg, n = (words[n] if n < len(words) else ""), n + 1
                        if letter == "u":
                            env[arg] = None
                        elif letter == "C":
                            cwd = _expand(arg, cwd)
                        break
        n += n < len(words) and words[n] == "--"
        while n < len(words) and _ENV_ASSIGN_RE.match(words[n]):
            name, value = words[n].split("=", 1)
            env[name], n = value, n + 1
        if n < len(words) and words[n].startswith("-"):
            return env, cleared, False, cwd
    return env, cleared, True, cwd


class _ShellVars:
    """The tracked variables as one shell sees them: a current value and, independently, whether it is exported."""

    def __init__(self, exported=None):
        self.value, self.exported = {}, {}
        for name, value in (exported or {}).items():  # None: unset by an enclosing `env -u` or `unset`
            self.value[name], self.exported[name] = value, value is not None

    def _get(self, name):
        if name in self.value:
            return self.value[name], self.exported.get(name, False)
        host = os.environ.get(name)  # the hook's own environment is what the shell started with
        return host, host is not None

    def environment(self):
        """What a command receives: exported values, None for a name that is set but not exported or unset."""
        return {name: value if self.exported.get(name) else None for name, value in self.value.items()}

    def assign(self, word, export=None):
        """`NAME=VAL` / `NAME+=VAL` / bare `NAME`, optionally exporting (True) or unexporting (False) it."""
        name, has_value, text = word.partition("=")
        append = has_value and name.endswith("+")
        name = name[:-1] if append else name
        if name not in _TRACKED:
            return
        current, exported = self._get(name)
        if has_value:
            self.value[name] = (current or "") + text if append else text
        else:
            self.value.setdefault(name, current)
        self.exported[name] = exported if export is None else export

    def unset(self, name):
        self.value[name], self.exported[name] = None, False

    def declare(self, builtin, args):
        """`export` / `declare` / `typeset`: `export` and -x export; `export -n` and `declare +x` stop exporting."""
        flags = "".join(w[1:] for w in args if re.match(r"^-[A-Za-z]+$", w))
        unflags = "".join(w[1:] for w in args if re.match(r"^\+[A-Za-z]+$", w))
        if builtin == "export":
            export = False if "n" in flags else True
        else:
            export = True if "x" in flags else False if "x" in unflags else None
        for word in args:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?(=|$)", word):
                self.assign(word, export)


class _Shell:
    """One possible state of the shell running a command list: its directory and variables."""

    def __init__(self, cwd, variables, unknown=False):
        self.cwd, self.variables, self.unknown = cwd, variables, unknown  # unknown: more states than are kept

    def copy(self):
        clone = _ShellVars()
        clone.value, clone.exported = dict(self.variables.value), dict(self.variables.exported)
        return _Shell(self.cwd, clone, self.unknown)

    def key(self):
        return (str(self.cwd), tuple(sorted(self.variables.value.items())), tuple(sorted(self.variables.exported.items())),
                self.unknown)


_MAX_SHELLS = 16  # possible states kept when conditional commands branch; beyond it one `unknown` state stands for the rest
UNKNOWN_MESSAGE = ("bash-guard: DENIED - cargo %s after more conditional cd/export/unset commands than the guard can follow; "
                   "run the build in a separate Bash command with CARGO_TARGET_DIR set to the shared target (%s)")
_OPENERS = frozenset(("if", "while", "until", "for", "select", "case", "{", "function", "coproc"))
_CLOSERS = frozenset(("fi", "done", "}", "esac"))


def main_checkout_cargo(command, cwd, depth=0, inherited=None):
    """Deny message for a cargo build whose target dir is a main checkout's or agent worktree's own, wherever cwd, `cd`, `-C` or `--manifest-path` put it.

    The shell state (directory, exported variables) follows the command list: a `cd` or `export` reaches later
    commands only from the current shell (not a subshell, `$( )`, pipeline or background job); one that may not
    run (after `&&` or `||`, inside `if`, a loop, a group) leaves both possibilities open and a build is
    refused when either would fill a checkout.
    """
    if depth > 3:
        return None
    from bash_guard_rules import _ENV_ASSIGN_RE, _SHELLS, _WRAPPERS, _basename, _shell_strings
    from bash_guard_rules2 import lead, scan_flow
    commands, flow = scan_flow(command)
    levels = [[_Shell(Path(cwd or Path.cwd()), _ShellVars(inherited))]]  # the possible states at each scope depth
    compound = [0]  # open `if` / loop / group constructs at each scope depth
    for cmd, (before, after, level, entry) in zip(commands, flow):
        if entry:  # the first command of a subshell or substitution starts from a copy of the enclosing state
            del levels[level:], compound[level:]
        while len(levels) <= level:
            levels.append([shell.copy() for shell in levels[-1]])
            compound.append(0)
        del levels[level + 1:], compound[level + 1:]
        shells = levels[level]
        pos = [w for w in cmd if not w.redir]
        words = [w.text for w in pos]
        i = lead(pos)
        first = words[0] if words else ""
        compound[level] += first in _OPENERS
        pipelined = before in ("|", "|&") or after in ("|", "|&", "&")  # runs in a subshell of its own
        conditional = before in ("&&", "||") or i > 0 or compound[level] > 0
        start = i
        while i < len(words) and _ENV_ASSIGN_RE.match(words[i]):
            i += 1
        prefix = dict(w.split("=", 1) for w in words[start:i])  # only the assignments before the command word
        changed = []  # the states this command leaves behind, were it to run
        for shell in shells:
            envs = {**shell.variables.environment(), **prefix}
            if i >= len(words) or _basename(words[i]) in ("unset", "export", "declare", "typeset", "cd", "pushd"):
                after_run = shell.copy()
                if i >= len(words):
                    for word in words[start:i]:  # a bare `NAME=VAL` changes a variable that is already exported
                        after_run.variables.assign(word)
                elif _basename(words[i]) == "unset":
                    for name in words[i + 1:]:
                        if not name.startswith("-"):
                            after_run.variables.unset(name)
                elif _basename(words[i]) in ("export", "declare", "typeset"):
                    after_run.variables.declare(_basename(words[i]), words[i + 1:])
                elif len(words) > i + 1:
                    after_run.cwd = _expand(words[i + 1], shell.cwd)  # later commands run there
                changed.append(after_run)
                continue
            reason = _check_run(words, pos, i, envs, shell, depth)
            if reason:
                return reason
            changed.append(shell)
        if not pipelined and changed != shells:
            kept = changed if not conditional else [*shells, *changed]
            unique = list({shell.key(): shell for shell in kept}.values())
            if len(unique) > _MAX_SHELLS:
                unique = [*unique[:_MAX_SHELLS - 1], _Shell(unique[0].cwd, _ShellVars(), unknown=True)]
            levels[level] = unique
        compound[level] = max(0, compound[level] - (first in _CLOSERS))
    return None


def _check_run(words, pos, i, envs, shell, depth):
    """Deny message when the command at words[i] (a wrapper, a shell or cargo itself) runs a cargo build into a checkout."""
    from bash_guard_rules import _SHELLS, _WRAPPERS, _basename, _shell_strings
    cwd = shell.cwd
    # Wrapper options are not modelled: every later word may be the command, as check_command reads it.
    wrapped = _basename(words[i]) in _WRAPPERS | _PLACEMENT_WRAPPERS or any(_basename(w) in _PGTEST for w in words[i:i + 3])
    evaled = False
    for j in range(i, len(words)) if wrapped else (i,):
        name = _basename(words[j])
        if name in _SHELLS:
            nested = _shell_strings(pos, j + 1)
        elif name == "eval" and not evaled:
            evaled, nested = True, [" ".join(words[j + 1:])]
        else:
            nested = []
        env, cleared, reached, where = _env_apply(words[i:j], envs, cwd)  # `env -u`, `-C`, `NAME=VAL` before this word
        if not reached:
            continue
        wrapped_env = {**{var: None for var in _TRACKED}, **env} if cleared else env
        for string in nested:
            reason = main_checkout_cargo(string, where, depth + 1, wrapped_env)
            if reason:
                return reason
        if name != "cargo":
            continue
        rest = words[j + 1:]
        if "--" in rest:  # what follows goes to the program cargo runs, not to cargo
            rest = rest[:rest.index("--")]
        sub = _cargo_subcommand(rest)
        if sub not in _BUILDING:
            continue
        if shell.unknown:
            return UNKNOWN_MESSAGE % (sub, shared_target())
        root = _cargo_target_checkout(rest, env, _cargo_workdir(rest, where), {} if cleared else None)
        if root is not None:
            return MAIN_TARGET_MESSAGE % (sub, root, shared_target())
    return None
