"""Private files, with permissions established before any content is written."""
import os
from pathlib import Path
import tempfile
import time


def atomic_write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(name, path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (32, 33) or attempt == 19:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        Path(name).unlink(missing_ok=True)


def append_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def create_private(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return secure_sqlite_files(path)
    os.close(fd)
    return True


def secure_sqlite_files(path):
    """Best-effort privacy for existing databases and SQLite sidecars."""
    import logging
    ok = True
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            os.chmod(str(path) + suffix, 0o600)
        except FileNotFoundError:
            continue
        except OSError as exc:
            ok = False
            logging.getLogger(__name__).warning('sqlite_permissions_failed: %s', type(exc).__name__)
    return ok
