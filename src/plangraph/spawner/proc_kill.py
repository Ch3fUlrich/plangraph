"""Terminate a run's isolated process group or Windows tree, and wait for death."""
import asyncio
import os
import time
from pathlib import Path

WAIT_S = 2.0


def group_alive(pgid):
    """True while any non-zombie member of the process group survives."""
    try:
        entries = list(Path('/proc').iterdir())
    except OSError:
        return False  # no /proc: the kill is fire-and-forget on this platform
    for path in entries:
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z' and int(fields[2]) == pgid:
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


async def kill_process_tree(pid, *, platform=None, command_factory=None):
    """Terminate the isolated POSIX group or Windows process tree."""
    if (platform or os.name) == 'nt':
        factory = command_factory or asyncio.create_subprocess_exec
        proc = await factory('taskkill', '/PID', str(pid), '/T', '/F',
                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()
        return
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return  # the run's process group is already gone
    # SIGKILL + wait: the reaped run must be seen dead, not merely signalled -
    # CI caught a grandchild still in state R while collect was returning.
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(group_alive, pid):
            return
        await asyncio.sleep(0.02)
