"""act.py - stop session execution runner (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import subprocess
from typing import Any, Callable, Optional

TIMEOUT_MSG = "timeout"


def default_runner(argv: list[str], timeout: float = 30.0) -> Any:
    """Default process runner using subprocess.run with argv list, no shell."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def stop_session(
    short_id: str,
    apply: bool = False,
    runner: Optional[Callable[..., Any]] = None,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """Stop a session via `claude stop <id>`.

    Args:
        short_id: The short session id.
        apply: If False (dry run), the runner is never invoked.
        runner: Injectable process runner callable (argv, timeout=...).
        timeout: Timeout in seconds for runner execution (default 30 s).

    Returns:
        (rc, message) where rc is the process return code (0 for dry run).
    """
    if not apply:
        return 0, "dry_run"

    if runner is None:
        runner = default_runner

    argv = ["claude", "stop", str(short_id)]
    try:
        proc = runner(argv, timeout=timeout)
        rc = getattr(proc, "returncode", None)
        # An unverifiable result must never read as a clean stop.
        if isinstance(rc, bool) or not isinstance(rc, int):
            return 1, "runner_error: no integer returncode"
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        msg = stdout if stdout else stderr
        return int(rc), str(msg).strip()
    except subprocess.TimeoutExpired:
        return 1, TIMEOUT_MSG
    except Exception as exc:
        # Runner failure: logged with rc, no crash
        return 1, f"runner_error: {exc}"


def failure_tag(rc: int, message: str) -> str:
    """Short, content-free tag for a failed stop ('' on success), for the audit line."""
    if rc == 0:
        return ""
    if message == TIMEOUT_MSG:
        return "timeout"
    if message.startswith("runner_error"):
        return "runner_error"
    return "nonzero_rc"
