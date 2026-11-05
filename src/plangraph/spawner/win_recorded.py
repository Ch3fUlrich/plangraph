"""Recorded Windows cancellation uses only a proven named Job Object."""
import asyncio
import ctypes
import os
import time
from ctypes import wintypes as w
from . import proc_kill
from .winjob import WinJobApi

if os.name == 'nt':
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = k32.OpenJobObjectW.restype = ctypes.c_void_p


def creation(pid, handle=None):
    if os.name != 'nt':
        return None
    owned = handle is None
    handle = handle or k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [w.FILETIME() for _ in range(4)]
        if k32.GetProcessTimes(ctypes.c_void_p(handle), *(ctypes.byref(t) for t in times)):
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        if owned:
            k32.CloseHandle(ctypes.c_void_p(handle))


async def kill(pid, token, name=None, escapees=()):
    if os.name != 'nt' or not name or type(token) is not int:
        return [pid]
    root = k32.OpenProcess(0x1000, False, pid)
    job = None
    try:
        if not root or creation(pid, root) != token:
            return [pid]
        job = k32.OpenJobObjectW(0x0008 | 0x0004, False, ctypes.c_wchar_p(name))
        member = w.BOOL()
        if not job or not k32.IsProcessInJob(ctypes.c_void_p(root), ctypes.c_void_p(job), ctypes.byref(member)) or not member.value:
            return [pid]
        api = WinJobApi()
        api.terminate(job)
        deadline = time.monotonic() + proc_kill.KILL_WAIT_S
        while True:
            live = set(api.pids(job)) | {p for p in escapees if proc_kill.alive(p, None)}
            if not live or time.monotonic() >= deadline:
                return sorted(live)
            await asyncio.sleep(.05)
    except OSError:
        return [pid]
    finally:
        for handle in (job, root):
            if handle:
                k32.CloseHandle(ctypes.c_void_p(handle))
