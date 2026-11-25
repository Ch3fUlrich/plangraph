"""Tool scope of a Claude harness run.

Sonnet permits test commands only when its sandbox can start. Even an allowed
test command runs code from the reviewed checkout.
"""

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path
from tempfile import mkdtemp

READ_TOOLS = "Read,Grep,Glob"
REVIEW_TOOLS = READ_TOOLS + ",Bash(python3 -m pytest -q *)"
REVIEW_DENIED = ("Write,Edit,Bash(python3 -m pytest -q -*),Bash(python3 -m pytest -q * -*),"
                 "Bash(python3 -m pytest -q /*),Bash(*..*),Bash(*$*),Bash(*`*),"
                 "Bash(*\"*),Bash(*'*),Bash(*\\*)")

_PYTEST = ("pytest", "python3 -m pytest")
_CARGO_TEST = ("cargo test -p", "cargo test --manifest-path rust/Cargo.toml -p")
_FAST_CHECKS = "plangraph fast-checks --base"
_SANDBOX_CACHE_SECONDS = 600


def secret_data():
    """The one data file of secret paths and tool-environment exclusions."""
    return json.loads(files("plangraph").joinpath("config/secret_paths.json").read_text())


def secret_paths(env=None):
    """Secret paths of the one deny list the Rust sandbox also reads, resolved against the
    effective config home: the default ``~/.config`` and a relocated ``XDG_CONFIG_HOME``."""
    data = secret_data()
    paths = [f"~/{rel}" for rel in data["home_relative"]]
    paths += [f"~/.config/{rel}" for rel in data["config_home_relative"]]
    xdg = (env or {}).get("XDG_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        paths += [str(Path(xdg) / rel) for rel in data["config_home_relative"]]
    return paths


def _credential_paths(env):
    paths = [*secret_paths(env), "~/.config", "~/.claude"]
    if config := (env or {}).get("CLAUDE_CONFIG_DIR"):
        paths.append(str(Path(config).resolve()))
    return paths


def read_deny_rules(paths):
    """Permission rules that stop the built-in Read, Grep and Glob tools on ``paths``; they
    hold whether or not the Bash sandbox starts. An absolute path needs the ``//`` prefix."""
    rules = []
    for path in paths:
        rule = "/" + path if path.startswith("/") else path
        rules += [f"Read({rule})", f"Read({rule}/**)"]
    return rules


def _review_command(command, worktree):
    if not re.fullmatch(r"[A-Za-z0-9_./: =+-]+", command):
        return False
    command = " ".join(command.split())
    for prefix in _PYTEST:
        if command.startswith(prefix + " "):
            args = iter(command.removeprefix(prefix + " ").split())
            paths = []
            for arg in args:
                if arg in {"-q", "-x", "-v", "--disable-warnings"}:
                    continue
                if arg == "-k" and re.fullmatch(r"[A-Za-z0-9_]+", next(args, "")):
                    continue
                # Judge the literal text: str(Path) uses backslashes on Windows.
                name = arg.partition("::")[0]
                if (not re.fullmatch(r"tests/[A-Za-z0-9_./-]+\.py", name)
                        or ".." in name.split("/")):
                    return False
                paths.append(worktree / name)
            return bool(paths) and all(
                path.resolve().is_relative_to(worktree) and path.is_file() for path in paths
            )
    for prefix in _CARGO_TEST:
        if command.startswith(prefix + " "):
            return bool(re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]*(?: --(?:lib|locked|offline)| --test [A-Za-z0-9_-]+)*"
                r"(?: [A-Za-z0-9_:]+)?(?: -- --(?:exact|nocapture))?",
                command.removeprefix(prefix + " "),
            ))
    return bool(re.fullmatch(re.escape(_FAST_CHECKS) + r" [0-9a-f]{40}", command))


def _review_bash_hook(worktree):
    """Block shell invocations outside the scoped test policy."""
    try:
        payload = json.load(sys.stdin)
        tool = payload["tool_input"]
        current = Path(payload["cwd"]).resolve(strict=True)
        if (payload["hook_event_name"] == "PreToolUse" and payload["tool_name"] == "Bash"
                and current == worktree and Path.cwd().resolve() == worktree
                and not tool.get("run_in_background") and not tool.get("dangerouslyDisableSandbox")
                and _review_command(tool["command"], worktree)):
            return 0
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
        pass
    print("Review Bash permits only scoped foreground tests in the review checkout.",
          file=sys.stderr)
    return 2


def _smoke_sandbox(command):
    try:
        return subprocess.run(command, cwd="/", env={}, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sandbox_available(env):
    """Reuse a host smoke result across the short-lived launch adapters."""
    if sys.platform == "linux":
        # A relative PATH entry could resolve to a program from the review checkout.
        search = os.pathsep.join(part for part in env.get("PATH", os.defpath).split(os.pathsep)
                                 if os.path.isabs(part))
        bwrap = shutil.which("bwrap", path=search) if search else None
        socat = shutil.which("socat", path=search) if search else None
        if not bwrap or not socat:
            return False
        command = [bwrap, "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/",
                   "--proc", "/proc", "--", "/bin/true"]
        dependencies = [bwrap, socat]
    elif sys.platform == "darwin":
        dependencies = ["/usr/bin/sandbox-exec"]
        command = [dependencies[0], "-p", "(version 1) (allow default) (deny network*)", "/usr/bin/true"]
    else:
        return False

    import fcntl

    # Recheck after an install or policy change. The lock makes simultaneous launches
    # share one probe, and a cache I/O failure leaves the actual smoke check available.
    available = None
    try:
        key = [socket.gethostname(), command,
               [[path, os.stat(path).st_mtime_ns] for path in dependencies]]
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", ""))
        if not cache_root.is_absolute():
            cache_root = Path.home() / ".cache"
        cache = cache_root / "plangraph" / "claude-sandbox.json"
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(cache, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            if 0 <= time.time() - os.fstat(fd).st_mtime < _SANDBOX_CACHE_SECONDS:
                try:
                    saved = json.loads(stream.read(16 * 1024))
                except ValueError:
                    saved = None
                if isinstance(saved, dict) and saved.get("key") == key and type(saved.get("ok")) is bool:
                    return saved["ok"]
            available = _smoke_sandbox(command)
            stream.seek(0)
            stream.write(json.dumps({"key": key, "ok": available}))
            stream.truncate()
            return available
    except OSError:
        return _smoke_sandbox(command) if available is None else available


def claude_access_args(role: str, tool_mode: str = "tools") -> list[str]:
    if role != "review":
        return ["--permission-mode", "plan"]
    return ["--permission-mode", "default", "--setting-sources", "user",
            "--allowedTools", REVIEW_TOOLS, "--disallowedTools", REVIEW_DENIED,
            "--tools", "" if tool_mode == "prompt-only" else READ_TOOLS + ",Bash",
            "--strict-mcp-config", "--mcp-config",
            str(files("plangraph").joinpath("config/empty-mcp.json")),
            "--disable-slash-commands"]


def merge_settings(argv, patch):
    """Merge ``patch`` into the JSON of the argv's ``--settings`` flag, or add the flag: lists
    are concatenated without duplicates, so a second caller never creates a second flag."""
    def merge(base, extra):
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            elif isinstance(value, list) and isinstance(base.get(key), list):
                base[key] = base[key] + [item for item in value if item not in base[key]]
            else:
                base[key] = value
        return base
    if "--settings" in argv:
        index = argv.index("--settings") + 1
        try:
            current = json.loads(argv[index])
        except ValueError:
            current = None
        if isinstance(current, dict):
            argv[index] = json.dumps(merge(current, patch))
            return
    argv.extend(["--settings", json.dumps(patch)])


def deny_secret_reads(argv, env):
    """Deny the built-in read tools on the secret paths for every Claude run, whatever its role:
    permission rules hold without the Bash sandbox and for the writer route."""
    merge_settings(argv, {"permissions": {"deny": read_deny_rules(_credential_paths(env))}})


def sandbox_denies_reads(env):
    """Smoke check that this host's sandbox really hides a file it is told to hide: ``/bin/true``
    starting proves nothing about denial. A fixture file is readable in a control run and gone
    in a run that masks its directory, the way Claude's sandbox masks a ``denyRead`` path."""
    if sys.platform == "linux":
        search = os.pathsep.join(part for part in env.get("PATH", os.defpath).split(os.pathsep)
                                 if os.path.isabs(part))
        bwrap = shutil.which("bwrap", path=search) if search else None
        if not bwrap:
            return False
    elif sys.platform != "darwin":
        return False
    directory = None
    try:
        directory = mkdtemp(prefix="plangraph-deny-smoke-")
        fixture = str(Path(directory) / "fixture-secret")
        Path(fixture).write_text("fixture")
        if sys.platform == "linux":
            base = [bwrap, "--unshare-pid", "--ro-bind", "/", "/", "--proc", "/proc"]
            visible = base + ["--", "/bin/sh", "-c", 'test -e "$1"', "sh", fixture]
            masked = base + ["--tmpfs", directory, "--", "/bin/sh", "-c", 'test ! -e "$1"', "sh", fixture]
        else:
            sandbox = "/usr/bin/sandbox-exec"
            read = ["/bin/sh", "-c", 'cat "$1" >/dev/null', "sh", fixture]
            refuse = ["/bin/sh", "-c", 'cat "$1" >/dev/null 2>&1 && exit 1; exit 0', "sh", fixture]
            visible = [sandbox, "-p", "(version 1) (allow default)", *read]
            masked = [sandbox, "-p", f'(version 1) (allow default) (deny file-read* (subpath "{directory}"))', *refuse]
        # The inversion lives in the shell, so a sandbox that fails to start is not a denial.
        return _smoke_sandbox(visible) and _smoke_sandbox(masked)
    except OSError:
        # No writable temporary directory is not a verified denial: the route is refused.
        return False
    finally:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


def confine_writer_shell(argv, env, role, job_id):
    """A routed Claude writer runs shell commands, which the Read deny rules do not reach: run
    them in Claude's filesystem sandbox with the secret paths unreadable, or refuse the route
    (the returned event carries ``refusal``) when this host cannot start that sandbox."""
    tools = argv[argv.index("--tools") + 1] if "--tools" in argv else "Bash"
    if role not in ("implement", "fix") or "Bash" not in tools.split(","):
        return None
    if not sandbox_available(env) or not sandbox_denies_reads(env):
        host = socket.gethostname()
        return {"event": "sandbox_unavailable", "host": host, "python_job_id": job_id,
                "refusal": f"sandbox_unavailable: claude shell commands cannot be confined on {host}; "
                           "the secret paths would stay readable"}
    # Only the settings built here apply: user, project and local settings could name sandbox
    # exclusions that run a command outside the sandbox.
    if "--setting-sources" in argv:
        argv[argv.index("--setting-sources") + 1] = ""
    else:
        argv.extend(["--setting-sources", ""])
    merge_settings(argv, {"sandbox": {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": False,
        "filesystem": {"denyRead": _credential_paths(env)}}})
    # An exclusion list already in an earlier --settings value is replaced, not merged.
    index = argv.index("--settings") + 1
    settings = json.loads(argv[index])
    settings["sandbox"]["excludedCommands"] = []
    argv[index] = json.dumps(settings)
    return None


def configure_review_tests(argv, env, leg, role, run_dirs, job_id, *,
                           worktree=None, review_target=None):
    """Apply Sonnet's review profile and return the read-only fallback event, if any."""
    from plangraph.planner.legs import REVIEW_SONNET

    if role != "review" or leg is None or leg.id != REVIEW_SONNET or leg.tool_mode != "tools":
        return confine_writer_shell(argv, env, role, job_id)
    argv[argv.index("--permission-mode") + 1] = "dontAsk"
    argv[argv.index("--setting-sources") + 1] = ""
    if not sandbox_available(env):
        host = socket.gethostname()
        for flag, value in [
            ("--allowedTools", READ_TOOLS), ("--disallowedTools", "Write,Edit,Bash,mcp__*"),
            ("--tools", READ_TOOLS),
        ]:
            argv[argv.index(flag) + 1] = value
        # Read, Grep and Glob stay, so the secret paths are denied by permission rule.
        deny_secret_reads(argv, env)
        argv.extend(["--restricted", "--append-system-prompt",
                     "Review with Read, Grep and Glob. Skip requests to run commands or tests. "
                     f"Write this exact standalone line: TESTS: none: sandbox unavailable on {host}"])
        return {"event": "sandbox_unavailable", "host": host, "python_job_id": job_id}
    target = Path(review_target).resolve() if review_target else None
    scratch = Path(mkdtemp(prefix="plangraph-claude-review-",
                           dir=target.parent if target else None)).resolve()
    run_dirs[job_id] = scratch
    target = target or (scratch / "cargo-target")
    target.mkdir(exist_ok=True)
    env.update(CARGO_TARGET_DIR=str(target), CARGO_BUILD_BUILD_DIR=str(target),
               CLAUDE_CODE_TMPDIR=str(scratch),
               TMPDIR=str(scratch), PYTHONDONTWRITEBYTECODE="1",
               PYTEST_ADDOPTS="-p no:cacheprovider", CARGO_NET_OFFLINE="true",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    # The installed service's venv is outside the disposable review clone.
    for bin_dir in (Path(sys.prefix) / "bin", Path(__file__).resolve().parents[3] / ".venv/bin"):
        if (bin_dir / "pytest").is_file():
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", os.defpath)
            break
    checkout = Path(worktree or Path.cwd()).resolve()
    hook = shlex.join([sys.executable, "-I", str(Path(__file__).resolve()),
                       "--review-bash", str(checkout)])
    credentials = _credential_paths(env)
    writable = [str(scratch)]
    if review_target:
        # The service's private build directory also holds the fast-checks pool lease.
        private_build = target.parent == checkout.parent / "build"
        writable.append(str(target.parent if private_build else target))
    env["PLANGRAPH_CARGO_TARGET_DIR"] = str(target)
    settings = {
        "permissions": {
            "allow": READ_TOOLS.split(",") + [
                f"Bash({prefix} *)" for prefix in (*_PYTEST, *_CARGO_TEST, _FAST_CHECKS)
            ],
            "deny": ["Write", "Edit", *read_deny_rules(credentials)],
        },
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": hook, "onFailure": "block"},
        ]}]},
        "sandbox": {
            "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False, "excludedCommands": [],
            "filesystem": {"denyWrite": ["."], "allowWrite": writable, "denyRead": credentials},
        },
    }
    for flag in ("--allowedTools", "--disallowedTools"):
        index = argv.index(flag)
        del argv[index:index + 2]
    merge_settings(argv, settings)
    argv.extend(["--restricted", "--append-system-prompt",
                 "Use Read, Grep and Glob to inspect the repository. Run scoped tests from the "
                 "review checkout, for example pytest -q tests/test_example.py or "
                 "cargo test --manifest-path rust/Cargo.toml -p CRATE TEST_FILTER. "
                 "Use one foreground command at a time with unquoted arguments. "
                 "Include the command and its verbatim summary on a standalone TESTS: line. "
                 "If no test ran, write TESTS: none: followed by the reason."])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--review-bash":
        sys.exit(_review_bash_hook(Path(sys.argv[2])))
    sys.exit(2)
