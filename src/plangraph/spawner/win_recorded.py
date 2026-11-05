"""Fail closed on unknown trees; only strictly later creation proves ancestry.
Hold first-observed handles so PID reuse cannot redirect termination.
"""
import ctypes
import os
from ctypes import wintypes as w

from . import proc_kill

if os.name == 'nt':
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p


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


async def kill(pid, token):
    members, handles, uncertain = {}, {}, set()
    def observe(target):
        handle = k32.OpenProcess(0x100001 | 0x1000, False, target)
        if handle:
            handles[target] = handle
        return creation(target, handle) if handle else None
    def live():
        table = proc_kill._nt_table()
        if not table:
            uncertain.add(pid)
        while True:
            added = False
            for child, parent in table.items():
                if parent in members and child not in handles and child not in uncertain:
                    start = observe(child)
                    if start is None or start == members[parent]:
                        uncertain.add(child)
                    elif start > members[parent]:
                        members[child] = start
                        added = True
            if not added:
                break
        return uncertain | {p for p, h in handles.items() if p in members and k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102}
    def terminate():
        for target in members:  # insertion order: root first, then descendants
            k32.TerminateProcess(ctypes.c_void_p(handles[target]), 1)
    try:
        table = proc_kill._nt_table()
        if type(token) is not int or pid not in table or observe(pid) != token or k32.WaitForSingleObject(ctypes.c_void_p(handles[pid]), 0) != 0x102:
            return [pid]
        members[pid] = token
        live()
        terminate()
        await proc_kill.finish(pid, token, {}, platform='nt', live_pids=live, reterminate=terminate)
        return sorted(uncertain)
    except proc_kill.KillIncomplete as exc:
        return exc.survivors
    finally:
        for handle in handles.values():
            k32.CloseHandle(ctypes.c_void_p(handle))
