"""Private files, with permissions established before any content is written."""
import os
from pathlib import Path
import tempfile


def atomic_write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
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
        secure_sqlite_files(path)
        return
    os.close(fd)


def secure_sqlite_files(path):
    """Best-effort privacy for existing databases and SQLite sidecars."""
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            os.chmod(str(path) + suffix, 0o600)
        except OSError:
            pass
