"""Tool scope of a Claude harness run.

Sonnet permits test commands only when its sandbox can start. Even an allowed
test command runs code from the reviewed checkout.
"""

import json
import os
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

_SANDBOX_CACHE_SECONDS = 600


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


def configure_review_tests(argv, env, leg, role, run_dirs, job_id, *, review_target=None):
    """Apply Sonnet's review profile and return the read-only fallback event, if any."""
    from plangraph.planner.legs import REVIEW_SONNET

    if role != "review" or leg is None or leg.id != REVIEW_SONNET or leg.tool_mode != "tools":
        return
    argv[argv.index("--permission-mode") + 1] = "dontAsk"
    argv[argv.index("--setting-sources") + 1] = ""
    if not sandbox_available(env):
        host = socket.gethostname()
        for flag, value in [
            ("--allowedTools", READ_TOOLS), ("--disallowedTools", "Write,Edit,Bash,mcp__*"),
            ("--tools", READ_TOOLS),
        ]:
            argv[argv.index(flag) + 1] = value
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
    allowed = ",".join([
        REVIEW_TOOLS, "Bash(pytest *)", "Bash(cargo test -p *)", "Bash(cargo build -p *)",
        "Bash(cargo test --manifest-path rust/Cargo.toml -p *)",
        "Bash(cargo build --manifest-path rust/Cargo.toml -p *)",
        "Bash(git log *)", "Bash(git show *)", "Bash(git diff *)", "Bash(git grep *)",
        "Bash(rg *)",
    ])
    credentials = ["~/.aws", "~/.ssh", "~/.gnupg", "~/.config", "~/.claude",
                   "~/.claude.json", "~/.git-credentials", "~/.netrc"]
    if config := env.get("CLAUDE_CONFIG_DIR"):
        credentials.append(str(Path(config).resolve()))
    writable = [str(scratch)]
    if review_target:
        writable.append(str(target))
    settings = {"sandbox": {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": False, "excludedCommands": [],
        "filesystem": {"denyWrite": ["."], "allowWrite": writable,
                       "denyRead": credentials},
    }}
    for flag, value in [
        ("--allowedTools", allowed),
        ("--disallowedTools", REVIEW_DENIED + ",Bash(git push *)"),
    ]:
        argv[argv.index(flag) + 1] = value
    argv.extend(["--restricted", "--settings", json.dumps(settings), "--append-system-prompt",
                 "Use one allowed Bash command at a time with unquoted arguments. For searches, "
                 "use single-word patterns. If no test ran, write TESTS: none: followed by the reason."])
