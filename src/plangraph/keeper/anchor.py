"""M2B-ANCHOR (D-789 D1): hash-chain anchors outside the keeper DB.

The keeper records the chain head (``seq``, ``hash``) in an append-only anchor log at
start, within one reap interval of every done transition, and hourly while the head
moves.  At start it refuses a DB whose head is behind the newest anchor (a rolled-back
file) or whose event at the anchored seq has another hash (a fork).

Config keys (all optional; none set = anchors off):

* ``anchor_socket`` + ``anchor_log``: production.  The keeper sends each record to the
  ``pganchor`` socket service (``anchord.py`` as user ``pganchor``, operator-installed);
  it reads ``anchor_log`` (world-readable, owned by ``pganchor``) for the start check.
* ``anchor_file``: interim, detection only.  The keeper appends itself; the operator
  sets ``chattr +a`` on the file (``pg admin verify-anchor`` reports the flag).
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime, timezone

from . import anchord

__all__ = ["AnchorError", "enabled", "read_records", "latest", "submit", "check_start",
           "verify", "append_only", "Anchorer", "HOURLY_S", "DONE_TYPES", "RETRY_AFTER_S"]

HOURLY_S = 3600
DONE_TYPES = ("review_passed", "closed_on_decision", "answered")
_SOCKET_TIMEOUT_S = 1.0     # held under the server lock: keep it short
RETRY_AFTER_S = 300         # after a failed write, the reaper skips anchoring this long


_UNSET = object()


class AnchorError(RuntimeError):
    """The DB contradicts the anchor log (rollback/fork) or an anchor cannot be written."""


def enabled(cfg) -> bool:
    return bool(cfg.get("anchor_socket") or cfg.get("anchor_file"))


def _log_path(cfg):
    path = cfg.get("anchor_log") if cfg.get("anchor_socket") else cfg.get("anchor_file")
    return os.path.expanduser(path) if path else None


def read_records(path):
    """(valid records in file order, count of malformed lines)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return [], 0
    except OSError as exc:                    # symlink (ELOOP), permissions: fail closed
        raise AnchorError(f"anchor log unreadable: {type(exc).__name__}") from exc
    with os.fdopen(fd, "rb") as handle:
        data = handle.read()
    records, bad = [], 0
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            records.append(anchord.parse(raw))
        except anchord.AnchorRefused:
            bad += 1
    return records, bad


def latest(path):
    records, _ = read_records(path)
    return max(records, key=lambda r: r["seq"]) if records else None


def _head(conn):
    row = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (row[0], row[1]) if row else (0, None)


def _hash_at(conn, seq):
    row = conn.execute("SELECT hash FROM events WHERE seq=?", (seq,)).fetchone()
    return row[0] if row else None


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0) \
        .isoformat().replace("+00:00", "Z")


def submit(cfg, rec) -> str:
    """Write one record through the socket service, or append to the interim file."""
    if cfg.get("anchor_socket"):
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_SOCKET_TIMEOUT_S)
        try:
            sock.connect(os.path.expanduser(cfg.get("anchor_socket")))
            sock.sendall(line)
            sock.shutdown(socket.SHUT_WR)
            answer = b""
            while len(answer) < 512:
                chunk = sock.recv(512)
                if not chunk:
                    break
                answer += chunk
        except OSError as exc:
            raise AnchorError(f"anchor service unreachable: {type(exc).__name__}") from exc
        finally:
            sock.close()
        text = answer.decode("ascii", "replace").strip()
        if not text.startswith("ok"):
            raise AnchorError(f"anchor service: {text or 'no answer'}")
        return text
    try:
        return anchord.append(_log_path(cfg), rec)
    except anchord.AnchorRefused as exc:
        raise AnchorError(f"anchor file: {exc}") from exc
    except OSError as exc:
        raise AnchorError(f"anchor file: {type(exc).__name__}") from exc


def check_start(conn, cfg):
    """Raise AnchorError when the DB contradicts ANY anchor (behind, forked, rewritten).

    Checking every record, not only the newest, means a forged higher anchor cannot
    whitewash a rewritten chain: the earlier genuine anchors still have to match.
    """
    if not enabled(cfg):
        return None
    if cfg.get("anchor_socket") and not cfg.get("anchor_log"):
        raise AnchorError("anchor_socket needs anchor_log (the file the start check reads)")
    path = _log_path(cfg)
    if cfg.get("anchor_socket") and not os.path.lexists(path):
        raise AnchorError(f"anchor log {path} missing (operator step A2 creates it): refused")
    records, bad = read_records(path)          # ONE read: the log cannot change between checks
    problems = _verify_records(conn, records, bad)
    last = max(records, key=lambda r: r["seq"]) if records else None
    if not records and bad:
        raise AnchorError(f"anchor log {path} has {bad} malformed line(s) and no valid record: refused")
    for problem in problems:
        if problem["code"] == "anchor_ahead":
            raise AnchorError(f"keeper.db head_seq {problem['db_head_seq']} is behind the anchored "
                              f"seq {problem['seq']}: rolled-back DB refused")
        if problem["code"] == "anchor_mismatch":
            raise AnchorError(f"keeper.db event {problem['seq']} differs from its anchor: "
                              "fork or rewrite refused")
    return last


def verify(conn, path):
    """Problems of the whole anchor log against the chain (empty = consistent)."""
    records, bad = read_records(path)
    return _verify_records(conn, records, bad)


def _verify_records(conn, records, bad):
    problems = [{"code": "anchor_malformed", "count": bad}] if bad else []
    seq_now, _ = _head(conn)
    prev = 0
    for rec in records:
        if rec["seq"] < prev:
            problems.append({"code": "anchor_order", "seq": rec["seq"], "after": prev})
        prev = max(prev, rec["seq"])
        if rec["seq"] > seq_now:
            problems.append({"code": "anchor_ahead", "seq": rec["seq"], "db_head_seq": seq_now})
        elif _hash_at(conn, rec["seq"]) != rec["head"]:
            problems.append({"code": "anchor_mismatch", "seq": rec["seq"]})
    return problems


def append_only(path):
    """True/False for the ext4/xfs append-only attribute (chattr +a), None if unknown."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        buf = fcntl.ioctl(fd, 0x80086601, struct.pack("l", 0))   # FS_IOC_GETFLAGS
        return bool(struct.unpack("l", buf)[0] & 0x20)            # FS_APPEND_FL
    except OSError:
        return None
    finally:
        os.close(fd)


class Anchorer:
    """Keeper-side schedule: an anchor at start, after done transitions, hourly."""

    def __init__(self, cfg, clock=time.time, err=sys.stderr, last=_UNSET):
        """*last*: the newest record (or None) from check_start's single read (the daemon
        passes it), so a record slipped in after the check is refused at the next write
        instead of silently raising the floor."""
        self.cfg, self.clock, self.err = cfg, clock, err
        if last is _UNSET:
            last = latest(_log_path(cfg))
        self.last_seq = last["seq"] if last else 0
        self.last_ts = self.clock() if last else 0.0
        self.error = None
        self.retry_at = 0.0

    def record(self, conn, reason):
        seq, head = _head(conn)
        if not head or seq <= self.last_seq:   # nothing new since the newest anchor
            return None
        if reason != "start" and self.clock() < self.retry_at:
            return None                        # back off: a down service must not stall requests
        rec = {"v": 1, "seq": seq, "head": head, "ts": _iso(self.clock()), "reason": reason}
        try:
            answer = submit(self.cfg, rec)
        except AnchorError as exc:
            if str(exc) != self.error:
                self.err.write(f"keeper: anchor not written: {exc}\n")
            self.error = str(exc)
            self.retry_at = self.clock() + RETRY_AFTER_S
            return None
        self.error, self.retry_at = None, 0.0
        self.last_seq, self.last_ts = seq, self.clock()
        return answer

    def tick(self, conn):
        seq, _ = _head(conn)
        if seq <= self.last_seq:
            return None
        placeholders = ",".join("?" for _ in DONE_TYPES)
        done = conn.execute(
            f"SELECT 1 FROM events WHERE seq > ? AND type IN ({placeholders}) LIMIT 1",
            (self.last_seq, *DONE_TYPES)).fetchone()
        if done:
            return self.record(conn, "done")
        if self.clock() - self.last_ts >= HOURLY_S:
            return self.record(conn, "hourly")
        return None
