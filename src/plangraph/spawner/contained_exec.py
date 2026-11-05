"""Linux per-run subreaper. Never adopts children of the service itself."""
import ctypes
import os
import subprocess
import sys
import asyncio
import signal
import time
from pathlib import Path

ORPHAN_GRACE_S = 10.0
CONTAINMENT_MARKER = 'plangraph: containment_unavailable'


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


def main(grace=ORPHAN_GRACE_S):
    # Refuse launch rather than create an uncontained run on restricted Linux.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        sys.stderr.write(CONTAINMENT_MARKER + '\n')
        return 125
    deadline = None
    child = None
    reaped = False
    forwarded = None

    def forward(sig, _frame):
        nonlocal deadline, forwarded
        forwarded = sig
        if deadline is None:
            deadline = time.monotonic() + grace
        if child is not None and not reaped:
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, forward)
    child = subprocess.Popen(sys.argv[1:], start_new_session=True)
    # Only this process's children are waited on: unrelated service children are
    # never adopted here. Keep the ownership anchor alive until all orphans reap.
    code = 0
    kill_deadline = None
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return code if reaped else 128 + (forwarded or signal.SIGKILL)
        if pid == child.pid:
            reaped = True
            code = os.waitstatus_to_exitcode(status)
            if code < 0:
                code = 128 - code
            if deadline is None:
                deadline = time.monotonic() + grace
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            if not reaped:
                try:
                    os.kill(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            # Only direct children of this private subreaper, including adopted
            # setsid daemons. Repeat as killed parents release their children.
            for path in Path('/proc/self/task').glob('*/children'):
                try:
                    children = path.read_text().split()
                except OSError:
                    continue
                for target in children:
                    try:
                        os.kill(int(target), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if kill_deadline is None:
                kill_deadline = now + 1.0
            if now >= kill_deadline:
                if not reaped:
                    # One bounded final reap; never report success for unknown status.
                    try:
                        pid, status = os.waitpid(child.pid, os.WNOHANG)
                    except ChildProcessError:
                        pid = 0
                    if pid == child.pid:
                        code = os.waitstatus_to_exitcode(status)
                        return code if code >= 0 else 128 - code
                    return 128 + (forwarded or signal.SIGKILL)
                return code
        time.sleep(0.01)


if __name__ == '__main__':
    sys.exit(main())
