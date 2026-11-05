"""Creation-time proof and handle-bound termination for recorded Windows trees."""
import ctypes
from ctypes import wintypes as w

from . import proc_kill

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.OpenProcess.restype = ctypes.c_void_p


def creation(pid, handle=None):
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


async def kill(pid, token):
    members, handles = {}, {}
    try:
        if type(token) is not int or creation(pid) != token:
            return [pid] if proc_kill.alive(pid, None) else []
        table = proc_kill._nt_table()
        if pid not in table:
            return [pid]
        members[pid] = token
        while True:
            added = False
            for child, parent in table.items():
                if parent in members and child not in members:
                    start = creation(child)
                    if start is None:
                        return sorted(set(members) | {child})
                    if start >= members[parent]:
                        members[child] = start
                        added = True
            if not added:
                break
        # Hold validated handles until verification: pid reuse cannot redirect a signal.
        for child, start in members.items():
            handle = k32.OpenProcess(0x100001 | 0x1000, False, child)
            if not handle or creation(child, handle) != start:
                if handle:
                    k32.CloseHandle(ctypes.c_void_p(handle))
                return sorted(members)
            handles[child] = handle
        for handle in reversed(list(handles.values())):
            k32.TerminateProcess(ctypes.c_void_p(handle), 1)
        await proc_kill.finish(pid, token, members, platform='nt', live_pids=lambda: [])
        return []
    except proc_kill.KillIncomplete as exc:
        return exc.survivors
    finally:
        for handle in handles.values():
            k32.CloseHandle(ctypes.c_void_p(handle))
