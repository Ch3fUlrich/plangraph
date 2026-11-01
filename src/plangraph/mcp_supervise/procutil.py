"""Process helpers: tree kill, liveness, start time, atomic per-server pid files."""

import functools
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Tuple

from .config import SERVER_NAME_RE

_STILL_ACTIVE = 259  # also a legal exit code, so liveness pairs it with a wait check
_WAIT_TIMEOUT = 0x102
_SYNCHRONIZE = 0x100000
_QUERY_LIMITED = 0x1000

# The supervisor's own heartbeat in state_dir. The leading underscore cannot collide
# with a server name (SERVER_NAME_RE needs an alphanumeric first character).
SUPERVISOR_PID_NAME = "_supervisor"

LogFn = Callable[[str], None]


def kill_process_tree(pid: int, log: Optional[LogFn] = None) -> bool:
    """Terminate one child and its descendants, never the caller's own group.

    Returns True if the tree is gone (or already was), False if the kill failed;
    the reason goes to `log`. Children are started with start_new_session=True, so
    on POSIX each one leads its own process group and killpg hits only that child's
    tree. A pid that is not a group leader (or whose group is ours) gets a plain
    kill instead. SIGKILL is not waited for: a zombie still answers `kill(pid, 0)`.
    """
    if pid <= 0:
        return False
    log = log or (lambda _msg: None)
    if sys.platform == "win32":
        try:
            done = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, check=False,
            )
        except Exception as e:
            log(f"taskkill pid={pid} failed to run: {type(e).__name__}")
            return not pid_alive(pid)
        if done.returncode == 0:
            return True
        if not pid_alive(pid):  # rc 128: it was already gone
            return True
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        log(f"taskkill pid={pid} rc={done.returncode}: {(detail[0] if detail else '')[:200]}")
        return False
    try:
        pgid = os.getpgid(pid)
        if pgid == pid and pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except Exception as e:
        log(f"kill pid={pid} failed: {type(e).__name__}")
        return False
    return True


@functools.lru_cache(maxsize=1)
def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    filetime = ctypes.POINTER(wintypes.FILETIME)
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE, filetime, filetime, filetime, filetime]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _win_open(pid: int, access: int):
    k32 = _kernel32()
    return k32, k32.OpenProcess(access, False, pid)


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running (never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TerminateProcess on Windows, so ask the kernel instead.
        import ctypes
        from ctypes import wintypes

        k32, handle = _win_open(pid, _QUERY_LIMITED | _SYNCHRONIZE)
        if not handle:
            k32, handle = _win_open(pid, _QUERY_LIMITED)  # SYNCHRONIZE may be denied
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            if code.value != _STILL_ACTIVE:
                return False
            # 259 is STILL_ACTIVE but also a real exit code: an exited process has a
            # signalled handle, a running one times out.
            return k32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start_time(pid: int) -> Optional[str]:
    """Opaque, stable creation stamp of a live process; None when it cannot be read.

    Two different processes never share (pid, stamp), which is what lets a stored
    pid be told apart from a recycled one.
    """
    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _win_start_time(pid)
        if os.path.exists(f"/proc/{pid}/stat"):
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
                # field 22 (starttime); the comm field may contain spaces, so split after ")"
                return f.read().rsplit(")", 1)[1].split()[19]
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def _win_start_time(pid: int) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    k32, handle = _win_open(pid, _QUERY_LIMITED)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = times[0]
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


def pid_matches(pid: int, start: Optional[str]) -> bool:
    """True if `pid` is alive and (when both stamps are known) still the same process."""
    if not pid_alive(pid):
        return False
    current = process_start_time(pid)
    return start is None or current is None or start == current


def read_pid_record(path: Path) -> Optional[Tuple[int, Optional[str]]]:
    """(pid, start stamp or None) from a pid file; a bare pid is the legacy form."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    pid_text, _, start = text.partition(" ")
    if not (pid_text.isdigit() and int(pid_text) > 0):
        return None
    return int(pid_text), (start.strip() or None)


def read_pid_file(path: Path) -> Optional[int]:
    record = read_pid_record(path)
    return record[0] if record else None


def write_pid_file(state_dir: Path, name: str, pid: Optional[int]) -> None:
    """Atomically write `<name>.pid` as "pid start-stamp"; pid None removes it."""
    if name != SUPERVISOR_PID_NAME and not SERVER_NAME_RE.fullmatch(name):
        raise ValueError(f"unsafe server name for a pid file: {name!r}")
    state_dir = Path(state_dir)
    target = state_dir / f"{name}.pid"
    if pid is None:
        target.unlink(missing_ok=True)
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    start = process_start_time(pid)
    tmp = state_dir / f"{name}.pid.{os.getpid()}.tmp"
    tmp.write_text(f"{pid} {start}" if start else str(pid), encoding="utf-8")
    os.replace(tmp, target)
