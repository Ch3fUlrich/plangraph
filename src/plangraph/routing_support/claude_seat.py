"""Tool scope of a Claude harness run.

The legacy seat has pytest on named paths. Sonnet uses a sandbox because even
an allowed test command runs code from the reviewed checkout.
"""

from importlib.resources import files
import json
import os
from pathlib import Path
import sys
from tempfile import mkdtemp

REVIEW_TOOLS = "Read,Grep,Glob,Bash(python3 -m pytest -q *)"
REVIEW_DENIED = ("Write,Edit,Bash(python3 -m pytest -q -*),Bash(python3 -m pytest -q * -*),"
                 "Bash(python3 -m pytest -q /*),Bash(*..*),Bash(*$*),Bash(*`*),"
                 "Bash(*\"*),Bash(*'*),Bash(*\\*)")


def claude_access_args(role: str, tool_mode: str = "tools") -> list[str]:
    if role != "review":
        return ["--permission-mode", "plan"]
    return ["--permission-mode", "default", "--setting-sources", "user",
            "--allowedTools", REVIEW_TOOLS, "--disallowedTools", REVIEW_DENIED,
            "--tools", "" if tool_mode == "prompt-only" else "Read,Grep,Glob,Bash",
            "--strict-mcp-config", "--mcp-config",
            str(files("plangraph").joinpath("config/empty-mcp.json")),
            "--disable-slash-commands"]


def configure_review_tests(argv, env, leg, role, run_dirs, job_id):
    """Apply the Sonnet review profile to the actual launch and its subprocesses."""
    from plangraph.planner.legs import REVIEW_SONNET

    if role != "review" or leg is None or leg.id != REVIEW_SONNET or leg.tool_mode != "tools":
        return
    scratch = Path(mkdtemp(prefix="plangraph-claude-review-")).resolve()
    run_dirs[job_id] = scratch
    target = scratch / "cargo-target"
    target.mkdir()
    env.update(CARGO_TARGET_DIR=str(target), CLAUDE_CODE_TMPDIR=str(scratch),
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
    settings = {"sandbox": {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": False, "excludedCommands": [],
        "filesystem": {"denyWrite": ["."], "allowWrite": [str(scratch)],
                       "denyRead": credentials},
    }}
    for flag, value in [
        ("--permission-mode", "dontAsk"), ("--setting-sources", ""),
        ("--allowedTools", allowed),
        ("--disallowedTools", REVIEW_DENIED + ",Bash(git push *)"),
    ]:
        argv[argv.index(flag) + 1] = value
    argv.extend(["--restricted", "--settings", json.dumps(settings), "--append-system-prompt",
                 "Use one allowed Bash command at a time with unquoted arguments. For searches, "
                 "use single-word patterns. If no test ran, write TESTS: none: followed by the reason."])
