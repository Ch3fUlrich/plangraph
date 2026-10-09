"""M2B-ANCHOR: append-only anchor log writer (stdlib only, no package imports).

The keeper records ``{"v": 1, "seq", "head", "ts", "reason"}`` lines - the hash-chain
head at a sequence number - so a rolled-back or forked ``keeper.db`` is refused at start.

Two ways to run it:

* as the ``pganchor`` service (operator installs a copy of THIS FILE, e.g.
  ``/usr/local/lib/pganchor/anchord.py``; ``pganchor@.service`` runs
  ``python3 anchord.py --log /var/lib/pganchor/anchor.log`` with the accepted socket
  connection on stdin/stdout).  The log belongs to the ``pganchor`` user, so the keeper's
  user cannot rewrite it: it can only ask this program to append a record that is not
  older than the last one.
* imported by the keeper's interim file mode (``anchor_file``), detection only.

One request = one JSON line (<= 4096 bytes); the answer is ``ok`` / ``ok duplicate`` or
``refused: <reason>``.  No shell, no subprocess, no network.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys

__all__ = ["MAX_LINE", "REASONS", "AnchorRefused", "parse", "valid", "last_record",
           "append", "serve_one", "main"]

MAX_LINE = 4096
REASONS = ("start", "done", "hourly")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TAIL = 256 * 1024


class AnchorRefused(Exception):
    """The record is malformed or older than the last anchored one."""


def valid(rec) -> bool:
    return (isinstance(rec, dict) and rec.get("v") == 1
            and isinstance(rec.get("seq"), int) and not isinstance(rec.get("seq"), bool)
            and rec["seq"] >= 1 and isinstance(rec.get("head"), str)
            and bool(_HEX64.match(rec["head"])) and isinstance(rec.get("ts"), str)
            and 0 < len(rec["ts"]) <= 40 and rec.get("reason") in REASONS
            and set(rec) == {"v", "seq", "head", "ts", "reason"})


def parse(line: bytes) -> dict:
    if len(line) > MAX_LINE:
        raise AnchorRefused("record too long")
    try:
        rec = json.loads(line.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnchorRefused("not a JSON record") from exc
    if not valid(rec):
        raise AnchorRefused("not an anchor record")
    return rec


def last_record(fd):
    """The highest-seq valid record in the log open on *fd* (scans the tail)."""
    size = os.fstat(fd).st_size
    start = max(0, size - _TAIL)
    os.lseek(fd, start, os.SEEK_SET)
    data = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    best = None
    for raw in data.split(b"\n"):
        try:
            rec = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        if valid(rec) and (best is None or rec["seq"] >= best["seq"]):
            best = rec
    return best


def append(path, rec: dict) -> str:
    """Append *rec* unless it is older than the last record; returns 'ok' or 'ok duplicate'."""
    if not valid(rec):
        raise AnchorRefused("not an anchor record")
    fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        last = last_record(fd)
        if last is not None:
            if rec["seq"] < last["seq"]:
                raise AnchorRefused(f"seq {rec['seq']} is older than the anchored {last['seq']}")
            if rec["seq"] == last["seq"]:
                if rec["head"] != last["head"]:
                    raise AnchorRefused(f"different head at the anchored seq {rec['seq']}")
                return "ok duplicate"
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        os.write(fd, line)
        os.fsync(fd)
        return "ok"
    finally:
        os.close(fd)


def serve_one(path, infile, outfile) -> int:
    line = infile.readline(MAX_LINE + 1)
    try:
        if not line.endswith(b"\n") and len(line) > MAX_LINE:
            raise AnchorRefused("record too long")
        answer, code = append(path, parse(line.strip())), 0
    except AnchorRefused as exc:
        answer, code = f"refused: {exc}", 1
    except OSError as exc:
        answer, code = f"refused: log error {type(exc).__name__}", 1
    outfile.write(answer.encode("ascii", "replace") + b"\n")
    outfile.flush()
    return code


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--log":
        sys.stderr.write("usage: anchord.py --log PATH\n")
        return 2
    return serve_one(args[1], sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
