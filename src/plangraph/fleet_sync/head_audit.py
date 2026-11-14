"""Self-contained, best-effort HEAD audit runtime copied outside the checkout.

Only stdlib imports: the installed copy must survive checking out older code.
The shell hooks select moves before starting this process. Failures are ignored.
"""


def process_chain():
    """Command names from /proc only; never arguments or environment."""
    import os
    from pathlib import Path

    chain, seen, pid = [], set(), os.getpid()
    while pid > 0 and pid not in seen and len(chain) < 128:
        seen.add(pid)
        try:
            stat = (Path('/proc') / str(pid) / 'stat').read_text()
            start, end = stat.index('('), stat.rindex(')')
            ppid = int(stat[end + 2:].split()[1])
            chain.append({'pid': pid, 'ppid': ppid, 'comm': stat[start + 1:end]})
            pid = ppid
        except (OSError, ValueError, IndexError):
            break
    return chain


def record(state, worktree, old, new, branch, gitdir, *, max_bytes=1024 * 1024):
    """Serialize append/rotation under the runtime deadline; never scan the log."""
    import fcntl
    import json
    import os
    import re
    from datetime import UTC, datetime
    from pathlib import Path

    if not all(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha) for sha in (old, new)):
        return
    # New Git versions report a zero old OID for symref -> direct HEAD.
    # Its just-written HEAD reflog retains the actual previous commit.
    if not old.strip('0'):
        try:
            with (Path(gitdir) / 'logs/HEAD').open('rb') as log:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 4096))
                fields = log.read().splitlines()[-1].split()
            if fields[1].decode('ascii') == new:
                old = fields[0].decode('ascii')
        except (OSError, ValueError, IndexError, UnicodeError):
            pass
    state = Path(state)
    row = {'time': datetime.now(UTC).isoformat(), 'old_sha': old, 'new_sha': new,
           'worktree': worktree, 'branch': branch, 'process_chain': process_chain()}
    line = (json.dumps(row, ensure_ascii=True) + '\n').encode()
    lock_fd = os.open(state / 'head-audit.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, 'wb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state / 'head-moves.jsonl'
        if path.exists() and path.stat().st_size + len(line) > max_bytes:
            os.replace(path, path.with_suffix('.jsonl.1'))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError('incomplete audit append')
        finally:
            os.close(fd)


if __name__ == '__main__':
    try:
        import signal

        # Arm before importing the runtime's dependencies or doing any I/O.
        # -I -S in the stub also bypasses site hooks and checkout PYTHONPATH.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        import sys

        record(*sys.argv[1:])
    except BaseException:  # noqa: BLE001, S110 -- audit must never veto Git
        pass
