"""Linux per-run subreaper. Never adopts children of the service itself."""
import ctypes
import os
import subprocess
import sys
import asyncio


async def create_subprocess_exec(*argv, **kwargs):
    return await asyncio.create_subprocess_exec(*command(list(argv)), **kwargs)


def command(argv):
    """Use a private supervisor on Linux; other hosts retain ordinary launch."""
    if sys.platform == 'linux':
        return [sys.executable, os.path.abspath(__file__), *argv]
    return argv


def supervisor(pid, start, proc_root='/proc'):
    """Recognize our wrapper only after checking its recorded identity."""
    from .proc_kill import _stat
    from pathlib import Path
    st = _stat(pid, proc_root)
    if st is None or st[4] != start:
        return False
    try:
        args = (Path(proc_root) / str(pid) / 'cmdline').read_bytes().split(b'\0')
        return len(args) > 1 and args[1] == os.fsencode(os.path.abspath(__file__))
    except OSError:
        return False


def main():
    # Refuse launch rather than create an uncontained run on restricted Linux.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        sys.stderr.write('containment unavailable: PR_SET_CHILD_SUBREAPER denied\n')
        return 125
    child = subprocess.Popen(sys.argv[1:])
    # Only this process's children are waited on: unrelated service children are
    # never adopted here. Keep the ownership anchor alive until all orphans reap.
    code = 0
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            return code
        if pid == child.pid:
            code = os.waitstatus_to_exitcode(status)
            if code < 0:
                code = 128 - code


if __name__ == '__main__':
    sys.exit(main())
