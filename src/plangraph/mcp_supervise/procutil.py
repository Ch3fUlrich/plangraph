"""Process helpers: tree kill, liveness, atomic per-server pid files."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

_STILL_ACTIVE = 259


def kill_process_tree(pid: int) -> None:
    """Terminate one child and its descendants, never the caller's own group.

    Children are started with start_new_session=True, so on POSIX each one leads
    its own process group and killpg hits only that child's tree. A pid that is
    not a group leader (or whose group is ours) gets a plain kill instead.
    """
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass
        return
    try:
        pgid = os.getpgid(pid)
        if pgid == pid and pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running (never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TerminateProcess on Windows, so ask the kernel instead.
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid_file(path: Path) -> Optional[int]:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() and int(text) > 0 else None


def write_pid_file(state_dir: Path, name: str, pid: Optional[int]) -> None:
    """Atomically write `<name>.pid` (temp file + replace); pid None removes it."""
    state_dir = Path(state_dir)
    target = state_dir / f"{name}.pid"
    if pid is None:
        target.unlink(missing_ok=True)
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / f"{name}.pid.{os.getpid()}.tmp"
    tmp.write_text(str(pid), encoding="utf-8")
    os.replace(tmp, target)
