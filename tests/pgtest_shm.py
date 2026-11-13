"""Dead-pid sweep of pg<pid>-* run and pgtest-<pid>-* worker directories."""
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
                m = re.fullmatch(r"(?:pg|pgtest-)(\d+)-.+", entry.name)
                if not m or not entry.is_dir(follow_symlinks=False):
                    continue
                pid = int(m.group(1))
                if not 0 < pid < 2**31 or pid == mypid:
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    shutil.rmtree(entry.path, ignore_errors=True)
                except OSError:
                    pass
            except OSError:
                continue
