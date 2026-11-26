"""Atomic ``plan.snapshot.json`` writer (+ M2B-SIGNED-SNAPSHOT detached signature)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import nullcontext
from itertools import count
from pathlib import Path

from . import snapshot_sign, timing

__all__ = ["write_snapshot", "read_snapshot", "SignError"]

SignError = snapshot_sign.SignError

#: The writer replaces the signature, then the body: between the two a reader sees (new sig, old
#: body).  The gap is microseconds, but a loaded reader can be descheduled across it, so the one
#: retry first waits this long.  Closing the gap for real needs a combined sig+body file, which
#: breaks snapshot_serve/snapshot_pull and every other reader of the two files (format change).
RETRY_PAUSE_S = 0.05


class SnapshotPublication:
    """Serialize publication and keep an older build from replacing a newer one."""

    def __init__(self, path=None, writer=None):
        from ..fleet_snapshot import FleetProjection

        self.fleet = FleetProjection()
        self.tickets = count()
        self.latest = self.requested = -1
        self.lock = threading.Lock()
        self.capture = threading.Lock()
        self._writer = writer
        self._writer_session = getattr(writer, "_raw", writer)
        self.source = _database_source(writer) if path else None

    def reserve(self):
        self.requested = next(self.tickets)
        return self.requested

    def publish(self, ticket, path, snapshot, key):
        with self.lock:
            if ticket > self.latest:
                _publish(path, snapshot, key)
                self.latest = ticket

    def validate_reader(self, conn):
        if (conn is self._writer or getattr(conn, "_raw", conn) is self._writer_session
                or _database_source(conn) != self.source or not conn.in_transaction):
            raise RuntimeError("snapshot requires a separate committed reader of the configured database")
        if self.source[0] == "postgres":
            receipt = conn.execute("SELECT current_schema(), current_setting('transaction_read_only'), "
                                   "current_setting('transaction_isolation')").fetchone()
            if tuple(receipt[i] for i in range(3)) != (self.source[1], "on", "repeatable read"):
                raise RuntimeError("snapshot requires a read-only repeatable read transaction")


def _database_source(conn):
    if hasattr(conn, "schema"):
        info = conn._raw.info
        return "postgres", conn.schema, info.host, info.hostaddr, info.port, info.dbname, info.user
    row = conn.execute("SELECT file FROM pragma_database_list WHERE name='main'").fetchone()
    return "sqlite", Path(row[0]).resolve() if row and row[0] else None


def write_snapshot(server, principal) -> None:
    """Rewrite ``plan.snapshot.json`` atomically (temp file then rename)."""
    publication = getattr(server, "snapshot_publication", None)
    lock = getattr(server, "lock", None)
    timer = timing._request_timer.get()
    owns_lock = getattr(lock, "_is_owned", lambda: False)()
    if getattr(server, "read_path", None):
        if owns_lock and (timer is None or timer.lock is not lock
                          or not isinstance(lock, timing.KeeperLock) or lock.depth() != 1
                          or server.store.conn.in_transaction):
            raise RuntimeError("configured snapshot requires a committed single-depth request or no request lock")
        with timing.outside_request_lock(lock) if owns_lock else nullcontext(), publication.capture:
            ticket = publication.reserve()
            with server.read_store(defer_audit=True) as store:
                publication.validate_reader(store.conn)
                snapshot = _capture(server, store, principal)
                hosts = store.list_bus_hosts()
            _write(server, snapshot, publication, ticket, hosts)
        return
    # In-process compatibility uses the shared store and keeps its lock throughout.
    scope = (timing.lock_scope(lock, "snapshot") if lock is not None and not owns_lock
             and (timer is None or timer.lock is not lock) else nullcontext())
    with scope:
        ticket = publication.reserve() if publication is not None else None
        snapshot = _capture(server, server.store, principal)
        _write(server, snapshot, publication, ticket)


def _capture(server, store, principal):
    # A run-token principal (M2B-RUN-TOKENS) may only act on its item: the snapshot is
    # role-agnostic, so it is built as the system principal, as the reaper does.
    from .auth import RunPrincipal
    who = server.system if isinstance(principal, RunPrincipal) else principal
    return timing.measured(part="build_ms")(store.snapshot)(who)


def _write(server, snapshot, publication, ticket, hosts=None):
    path = Path(server.config.snapshot)
    try:
        from ..fleet_snapshot import load_fleet
        load = timing.measured(part="fleet_ms")(load_fleet)
        if publication is None:
            snapshot["fleet"] = load(server.store)
        else:
            if hosts is None:
                hosts = server.store.list_bus_hosts()
            snapshot["fleet"] = load(None, registry=hosts, projection=publication.fleet)
    except Exception as exc:  # noqa: BLE001 - optional fleet must never fail a snapshot
        # Exception messages can contain private store paths; use a fixed summary.
        logging.getLogger(__name__).warning(
            "Fleet snapshot projection failed: %s: projection unavailable", type(exc).__name__)
        snapshot["fleet"] = None
        snapshot["fleet_error"] = "projection_failed"
    key = getattr(server.config, "signing_key", None)
    if publication is None:
        _publish(path, snapshot, key)
    else:
        publication.publish(ticket, path, snapshot, key)


def _publish(path, snapshot, key):
    body = timing.measured(part="encode_ms")(lambda: json.dumps(snapshot).encode("utf-8"))()
    path.parent.mkdir(parents=True, exist_ok=True)
    if key:   # M2B: signature then snapshot; a reader in the gap retries once. A crash between the
              # two writes leaves a mismatch that FAILS CLOSED until the next publication,
              # never an unsigned accept.
        signature = timing.measured(part="sign_ms")(snapshot_sign.sign)(body, key).encode()
        timing.measured(part="write_ms")(_atomic_write)(snapshot_sign.sig_path(path), signature)
    timing.measured(part="write_ms")(_atomic_write)(path, body)


def _atomic_write(path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_snapshot(path, pubkey=None, *, _retry=True) -> dict:
    """The local snapshot; with *pubkey* its detached signature must verify (M2B).

    Raises ``FileNotFoundError`` when absent and ``snapshot_sign.SignError`` when a
    configured key does not verify (unsigned, tampered or another key).  A reader
    racing the writer (new signature, old snapshot) retries once.
    """
    path = Path(path)
    body = path.read_bytes()
    if pubkey:
        sig = snapshot_sign.sig_path(path)
        try:
            signature = sig.read_bytes().decode("ascii") if sig.is_file() else ""
        except UnicodeDecodeError:
            signature = ""          # garbage .sig = invalid, never a crash
        if not snapshot_sign.verify(body, signature, pubkey):
            if _retry:
                time.sleep(RETRY_PAUSE_S)
                return read_snapshot(path, pubkey, _retry=False)
            raise snapshot_sign.SignError(f"snapshot signature invalid or missing: {path}")
    return json.loads(body.decode("utf-8"))
