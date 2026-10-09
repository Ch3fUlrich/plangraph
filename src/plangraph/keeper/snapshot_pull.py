"""PG-LOCAL-COPY (D-818): ``pg sync pull`` - refresh this host's local plan copy.

Fetches the keeper's signed snapshot over ssh (the pull key's forced command is
``snapshot_serve``), verifies the signature with ``snapshot_pubkey``, refuses a copy
older than the newest one this host has seen (``head_seq`` high-water mark, kept in
``<snapshot>.hwm``) or a different head at the same sequence, then installs it:
high-water mark, signature, snapshot, each by temp file + fsync + rename + directory
fsync, in that order.  A crash leaves the previous copy (stale, still signed) or a
signature mismatch the reader refuses (fail closed), never an unverified copy, and the
mark keeps a later pull from accepting anything older.  Reader config (mode 600)::

    {"snapshot": "~/.local/share/plangraph/plan.snapshot.json",
     "snapshot_pubkey": "~/.config/plangraph/snapshot-signing.pub",
     "pull": {"host": "code.vm address", "user": "s",
              "identity": "~/.ssh/pg-snapshot-pull", "port": 22}}
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from . import snapshot_serve, snapshot_sign

__all__ = ["PullError", "Unreachable", "pull", "ssh_argv", "hwm_path", "behind_mark", "TIMEOUT_S"]

TIMEOUT_S = 120
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")


class PullError(Exception):
    """The fetched copy is refused (signature, format, rollback, fork) or the config is bad."""


class Unreachable(Exception):
    """ssh failed or timed out; the local copy is unchanged."""


def hwm_path(snapshot) -> Path:
    return Path(str(snapshot) + ".hwm")


def behind_mark(snapshot, snap) -> bool:
    """True when a high-water mark exists and *snap* (the parsed local copy) is older than it
    or unreadable as such: a pull crashed between the mark and the snapshot (fail closed)."""
    hwm = hwm_path(snapshot)
    if not (hwm.exists() or hwm.is_symlink()):
        return False
    try:
        fd = os.open(str(hwm), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            mark = json.loads(handle.read(4096).decode("utf-8"))["head_seq"]
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return True
    seq = snap.get("head_seq") if isinstance(snap, dict) else None
    return not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(mark, int) or seq < mark


def ssh_argv(pull: dict) -> list:
    """The ssh command line; every value is validated so none can become an option."""
    if not isinstance(pull, dict):
        raise PullError("config 'pull' must be an object with host, user, identity")
    host, user, identity = pull.get("host"), pull.get("user"), pull.get("identity")
    port = pull.get("port", 22)
    if not isinstance(host, str) or not _HOST.match(host):
        raise PullError("pull.host must be a plain host name or address")
    if not isinstance(user, str) or not _USER.match(user):
        raise PullError("pull.user must be a plain user name")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise PullError("pull.port must be an integer 1-65535")
    if not isinstance(identity, str) or not identity or "\n" in identity:
        raise PullError("pull.identity must be a key file path")
    identity = str(Path(identity).expanduser().resolve())
    return ["ssh", "-T", "-x", "-a", "-F", "none",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes",
            "-o", "RequestTTY=no", "-o", "ConnectTimeout=15", "-o", "PermitLocalCommand=no",
            "-i", identity, "-p", str(port), "-l", user, "--", host]


def _fetch(argv, timeout=TIMEOUT_S) -> bytes:
    limit = snapshot_serve.MAX_BYTES * 4 // 3 + 4096   # base64 envelope of the largest snapshot
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=err)
        except OSError as exc:
            raise Unreachable(f"cannot run ssh: {exc}") from exc
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            data = proc.stdout.read(limit + 1)
            if len(data) > limit:
                proc.kill()
                raise PullError("keeper answer larger than the pull limit")
            code = proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
        if code != 0:
            err.seek(0)
            detail = err.read(300).decode("utf-8", "replace").strip().splitlines()
            raise Unreachable(f"ssh exit {code}: {detail[-1] if detail else 'no detail'}")
    return data


def _parse(data: bytes, pubkey):
    try:
        env = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PullError("keeper answer is not a pull envelope") from exc
    if (not isinstance(env, dict) or set(env) != {"format", "snapshot", "sig"}
            or env["format"] != snapshot_serve.FORMAT
            or not isinstance(env["snapshot"], str) or not isinstance(env["sig"], str)):
        raise PullError("keeper answer is not a pull envelope")
    try:
        body = base64.b64decode(env["snapshot"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PullError("snapshot is not valid base64") from exc
    if not snapshot_sign.verify(body, env["sig"], pubkey):
        raise PullError("snapshot signature invalid (unsigned, tampered or another key)")
    try:
        snap = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PullError("signed snapshot is not JSON") from exc
    seq, head = (snap.get("head_seq"), snap.get("head")) if isinstance(snap, dict) else (None, None)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or not isinstance(head, (str, type(None))):
        raise PullError("signed snapshot has no head_seq/head (keeper too old?)")
    return body, env["sig"], seq, head


def _local_seq(path, pubkey):
    from .snapshot_io import read_snapshot
    try:
        snap = read_snapshot(path, pubkey)
    except (OSError, snapshot_sign.SignError, ValueError):
        return -1, None
    seq = snap.get("head_seq") if isinstance(snap, dict) else None
    return (seq, snap.get("head")) if isinstance(seq, int) and not isinstance(seq, bool) else (-1, None)


def _current(path, pubkey):
    """(head_seq, head) this host has already accepted: the higher of the high-water mark and
    the verified local copy, so neither a lost nor a lowered mark lets an older copy back in."""
    best = _local_seq(path, pubkey)
    hwm = hwm_path(path)
    if hwm.exists() or hwm.is_symlink():
        try:
            fd = os.open(str(hwm), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                mark = json.loads(handle.read(4096).decode("utf-8"))
            seq, head = mark["head_seq"], mark["head"]
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ValueError("bad head_seq")
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise PullError(f"high-water mark {hwm} unreadable; check it, then remove it to re-seed") from exc
        if seq > best[0]:
            best = (seq, head)
    return best


def _install(path: Path, data: bytes) -> None:
    tmp = Path(f"{path}.{os.urandom(8).hex()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)   # each rename durable before the next: the order survives a power cut
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fsync_dir(directory) -> None:
    dfd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def pull(cfg, *, fetch=_fetch) -> str:
    """Refresh the local copy from the keeper; returns a one-line result."""
    pubkey = getattr(cfg, "snapshot_pubkey", None)
    if not pubkey:
        raise PullError("snapshot_pubkey is required: an unverified copy is never installed")
    argv = ssh_argv(cfg.get("pull"))
    path = Path(cfg.snapshot)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(f"{path}.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PullError("another pull is running") from exc
        body, sig, seq, head = _parse(fetch(argv), pubkey)
        cur_seq, cur_head = _current(path, pubkey)
        if seq < cur_seq:
            raise PullError(f"refused: keeper copy head_seq {seq} is older than this host's {cur_seq}")
        if seq == cur_seq and head != cur_head:
            raise PullError(f"refused: different head at the same head_seq {seq}")
        # High-water mark first: a crash after it can never let an older copy back in.
        _install(hwm_path(path), json.dumps({"head_seq": seq, "head": head}).encode("utf-8"))
        _install(snapshot_sign.sig_path(path), sig.encode("ascii"))
        _install(path, body)
    finally:
        os.close(lock_fd)
    return f"local copy at head_seq {seq}" + (" (unchanged)" if seq == cur_seq else f" (was {max(cur_seq, 0)})")
