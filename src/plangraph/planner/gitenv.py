from __future__ import annotations

import os
import subprocess
from pathlib import Path

from plangraph.keeper.errors import Refused

__all__ = ["git_env", "rev_parse"]

def git_env() -> dict[str, str]:
    # Minimal scrubbed env: repo-local GIT_DIR and friends never leak in.
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }

def rev_parse(root: str | Path, ref: str) -> str | None:
    cmd = [
        "git",
        "-C",
        str(root),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "rev-parse",
        "--verify",
        "--quiet",
        f"{ref}^{{commit}}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        raise Refused("unavailable", "git rev-parse failed") from None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
