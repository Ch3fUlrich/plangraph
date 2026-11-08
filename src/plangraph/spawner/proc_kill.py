"""Kill only the process tree a run started, then prove it is gone.

The root is proven ours at spawn (``root_identity``); a snapshot of its
descendants is taken before any signal, every kill is checked against the
snapshot's (pid, start time), and ``verify`` reports survivors.  A root that
is not proven ours is never snapshotted and never killed.
"""
import asyncio
import os
import time
import sys
import json
from pathlib import Path

WAIT_S = 2.0
KILL_WAIT_S = 10.0


def raise_oom_score(path=Path('/proc/self/oom_score_adj')):
    """Raise only; injectable proc file keeps policy tests hermetic."""
    try:
        current = int(path.read_text().strip())
        if current < 500:
            path.write_text('500')
        return dict(event='oom_score_adj', value=max(current, 500))
    except PermissionError:
        return dict(event='oom_score_adj_denied', errno=13)
    except (OSError, ValueError) as error:
        return dict(event='oom_score_adj_failed', errno=getattr(error, 'errno', None))


async def oom_process_factory(*args, **kwargs):
    path = kwargs.pop('oom_path', '/proc/self/oom_score_adj')
    if sys.platform.startswith('linux'):
        # Execute a fresh interpreter before policy I/O: no Python after fork.
        # A file entry point also works with env_clear and arbitrary child cwd.
        args = (sys.executable, str(Path(__file__).resolve()), str(path), *args)
    return await asyncio.create_subprocess_exec(*args, **kwargs)


class KillIncomplete(RuntimeError):
    """Processes of the run's tree outlived the kill; ``survivors`` are their pids."""

    def __init__(self, survivors):
        self.survivors = sorted(survivors)
        super().__init__('kill_incomplete: %d survivor(s)' % len(self.survivors))


def _stat(pid, proc_root='/proc'):
    """(state, ppid, pgrp, sid, starttime) of a pid, or None when unreadable."""
    try:
        f = (Path(proc_root) / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return f[0], int(f[1]), int(f[2]), int(f[3]), int(f[19])
    except (OSError, ValueError, IndexError):
        return None


def _scan(proc_root='/proc'):
    out = {}
    try:
        names = [p.name for p in Path(proc_root).iterdir() if p.name.isdigit()]
    except OSError:
        return out
    for name in names:
        st = _stat(name, proc_root)
        if st is not None:
            out[int(name)] = st
    return out


def group_alive(pgid):
    """True while any non-zombie member of the process group survives."""
    return any(s[0] != 'Z' and s[2] == pgid for s in _scan().values())


def group_members(pgid, *, proc_root='/proc'):
    """Live non-zombie pids whose process group is ``pgid`` (a pgid is never reused while populated)."""
    return sorted(p for p, s in _scan(proc_root).items() if s[0] != 'Z' and s[2] == pgid)


def escapees(root, job_pids, *, table=None):
    """nt: {pid: ppid} of root's descendants alive right after job assignment but not in the job.

    Processes created before AssignProcessToJobObject are not job members; they are
    recorded here and killed/verified at reap.  Raises KillIncomplete when the table is unreadable."""
    table = table if table is not None else _nt_table()
    if not table:
        raise KillIncomplete([root])
    found, grew = {root}, True
    while grew:
        kids = {p for p, pp in table.items() if pp in found and p not in found}
        grew = bool(kids)
        found |= kids
    inside = {int(p) for p in job_pids} | {root}
    return {p: table[p] for p in found if p not in inside}


async def verify_gone(pids, *, platform=None, wait_s=None):
    """Poll until none of ``pids`` is alive; KillIncomplete with the stragglers at the deadline."""
    deadline = time.monotonic() + (KILL_WAIT_S if wait_s is None else wait_s)
    while True:
        left = {p for p in pids if alive(p, None, platform=platform)}
        if not left:
            return
        if time.monotonic() >= deadline:
            raise KillIncomplete(left)
        await asyncio.sleep(0.05)


def root_identity(pid, *, platform=None, proc_root='/proc'):
    """Start-time token proving ``pid`` is our direct child and its own group leader.

    None means "not proven ours": nothing is snapshotted or killed for it."""
    if (platform or os.name) == 'nt':
        from .win_recorded import creation
        return creation(pid) if isinstance(pid, int) and _nt_table().get(pid) == os.getpid() else None
    st = _stat(pid, proc_root) if isinstance(pid, int) else None
    return st[4] if st and st[1] == os.getpid() and st[2] == pid else None


def _nt_table():
    """{pid: ppid} through Toolhelp32 (stdlib ctypes); {} off Windows."""
    try:
        import ctypes
        from ctypes import wintypes as w

        class Entry(ctypes.Structure):
            _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('pid', w.DWORD),
                        ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                        ('ppid', w.DWORD), ('pri', ctypes.c_long), ('flags', w.DWORD),
                        ('exe', ctypes.c_wchar * 260)]
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snap = ctypes.c_void_p(k32.CreateToolhelp32Snapshot(2, 0))
        table, entry = {}, Entry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.pid] = entry.ppid
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        k32.CloseHandle(snap)
        return table
    except Exception:
        return {}


def _nt_alive(pid):
    try:
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False


def snapshot(root, ident, *, platform=None, job_pids=None, proc_root='/proc', nt_table=None):
    """{pid: starttime} of the run's tree, taken before any kill.

    posix: the root, its process group and session, and every ppid-descendant of
    those (a setsid()'d grandchild still has its parent in the set).  nt: Job
    Object members when a job exists, else the Toolhelp32 ppid closure (only when
    the root is our child).  Empty when ``ident`` does not prove the root ours.

    Caveat (known, pre-existing): a descendant that setsid()s away and is orphaned
    after its root exits is invisible here; reap.sample_tree narrows the window, but
    closing it fully needs a subreaper (PR_SET_CHILD_SUBREAPER) or a cgroup."""
    if ident is None:
        return {}
    if (platform or os.name) == 'nt':
        if job_pids is not None:   # the live Job Object is the proof; empty means the job is empty
            return {int(p): None for p in job_pids}
        table = (nt_table or _nt_table)()
        if not table or table.get(root) != os.getpid():
            raise KillIncomplete([root])   # snapshot failed / root not provably our child: fail closed
        found = {root}
        grew = True
        while grew:
            kids = {p for p, pp in table.items() if pp in found and p not in found}
            grew = bool(kids)
            found |= kids
        return {p: None for p in found}
    procs = _scan(proc_root)
    found = {p for p, s in procs.items() if s[0] != 'Z' and root in (s[2], s[3])}
    if root in procs and procs[root][4] != ident:
        return {}  # the root pid was reused by a foreign process
    found = {p for p in found if p != root or procs[p][4] == ident}
    grew = True
    while grew:
        kids = {p for p, s in procs.items() if s[1] in found and p not in found and p != root}
        grew = bool(kids)
        found |= kids
    return {p: procs[p][4] for p in found if procs[p][0] != 'Z'}


def alive(pid, start, *, platform=None, proc_root='/proc'):
    """True while ``pid`` is the same live non-zombie process recorded at ``start``."""
    if (platform or os.name) == 'nt':
        return _nt_alive(pid)
    st = _stat(pid, proc_root)
    return st is not None and st[0] != 'Z' and (start is None or st[4] == start)


def _winpid(pid, proc_root):
    """MSYS pid -> Windows pid (taskkill speaks Windows pids)."""
    try:
        return int((Path(proc_root) / str(pid) / 'winpid').read_text().strip())
    except (OSError, ValueError):
        return pid


async def kill_process_tree(pid, *, platform=None, command_factory=None, members=None,
                            proc_root='/proc', ident=None):
    """Terminate the run's POSIX group / Windows tree; ``members`` is a pre-kill snapshot.

    No signal without proof of ownership (nt: proven members, never a raw pid; posix:
    positive pid, ``ident`` token, pid still the group leader); else KillIncomplete."""
    import signal
    if (platform or os.name) == 'nt':
        if not members:
            raise KillIncomplete([pid] if isinstance(pid, int) else [])
        factory = command_factory or asyncio.create_subprocess_exec
        for target in sorted(members):
            proc = await factory('taskkill', '/PID', str(_winpid(target, proc_root)),
                                 '/T', '/F', stdout=asyncio.subprocess.PIPE,
                                 stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
        return
    if type(pid) is not int or pid <= 1 or ident is None:
        raise KillIncomplete([pid] if type(pid) is int and pid > 1 else [])
    st = _stat(pid, proc_root)
    if st is not None and (st[4] != ident or st[2] != pid):
        raise KillIncomplete([pid])   # reused pid / not the group this run created
    if st is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # the run's process group is already gone
    for member, start in (members or {}).items():
        if alive(member, start, proc_root=proc_root):
            try:
                os.kill(member, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    # SIGKILL + wait: the reaped run must be seen dead, not merely signalled -
    # CI caught a grandchild still in state R while collect was returning.
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(group_alive, pid):
            return
        await asyncio.sleep(0.02)


async def verify(root, ident, members, *, platform=None, job_pids=None, proc_root='/proc',
                 wait_s=KILL_WAIT_S, live_pids=None, reterminate=None):
    """Poll until every snapshot member is gone, re-enumerating late forks/job members each pass.

    A snapshot that cannot be taken is not success: KillIncomplete."""
    import signal
    nt = (platform or os.name) == 'nt'
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if nt and live_pids is not None:
                late = {int(p): None for p in await asyncio.to_thread(live_pids)}
            else:
                late = await asyncio.to_thread(snapshot, root, ident, platform=platform,
                                               job_pids=job_pids, proc_root=proc_root)
        except KillIncomplete:
            raise
        except Exception:
            raise KillIncomplete(set(members) or {root})
        left = {p for p, s in members.items() if alive(p, s, platform=platform, proc_root=proc_root)}
        if nt:
            left |= set(late)   # a live job member is alive by definition
            if left and reterminate is not None:
                try:
                    reterminate()
                except Exception:
                    pass  # still-alive members are reported at the deadline
        else:
            for pid, start in late.items():
                if pid not in members and alive(pid, start, proc_root=proc_root):
                    left.add(pid)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
        if not left:
            return
        if time.monotonic() >= deadline:
            raise KillIncomplete(left)
        await asyncio.sleep(0.05)


async def finish(root, ident, members, *, platform=None, command_factory=None,
                 proc_root='/proc', wait_s=KILL_WAIT_S, live_pids=None, reterminate=None):
    """After the root-level kill: kill snapshot members still alive, then prove the tree gone."""
    left = {p: s for p, s in members.items() if alive(p, s, platform=platform, proc_root=proc_root)}
    if left and live_pids is None:
        await kill_process_tree(root, platform=platform, command_factory=command_factory,
                                members=left, proc_root=proc_root, ident=ident)
    await verify(root, ident, members, platform=platform, proc_root=proc_root, wait_s=wait_s,
                 live_pids=live_pids, reterminate=reterminate)


if __name__ == '__main__':
    event = raise_oom_score(Path(sys.argv[1]))
    os.write(2, (json.dumps({'plangraph_oom': event}) + '\n').encode())
    os.execvp(sys.argv[2], sys.argv[2:])
