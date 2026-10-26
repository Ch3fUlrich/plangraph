"""Dead-pid sweep of /dev/shm/pg<pid>-* run dirs; not in conftest (name clash with tests/contract)."""
import os
import re
import shutil


def _sweep_dead_pgtest_dirs(root) -> None:
    try:
        entries = os.scandir(root)
    except OSError:
        return
    mypid = os.getpid()
    with entries:
        for entry in entries:
            try:
                m = re.fullmatch(r"pg(\d+)-.+", entry.name)
                if not m or not entry.is_dir(follow_symlinks=False):
                    continue
                pid = int(m.group(1))
                if pid <= 0 or pid == mypid:
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    shutil.rmtree(entry.path, ignore_errors=True)
                except OSError:
                    pass
            except OSError:
                continue
