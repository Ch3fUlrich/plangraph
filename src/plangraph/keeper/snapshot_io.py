"""Atomic ``plan.snapshot.json`` writer (+ M2B-SIGNED-SNAPSHOT detached signature)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import snapshot_sign

__all__ = ["write_snapshot", "read_snapshot", "SignError"]

SignError = snapshot_sign.SignError

#: The writer replaces the signature, then the body: between the two a reader sees (new sig, old
#: body).  The gap is microseconds, but a loaded reader can be descheduled across it, so the one
#: retry first waits this long.  Closing the gap for real needs a combined sig+body file, which
#: breaks snapshot_serve/snapshot_pull and every other reader of the two files (format change).
RETRY_PAUSE_S = 0.05


def write_snapshot(server, principal) -> None:
    """Rewrite ``plan.snapshot.json`` atomically (temp file then rename)."""
    path = Path(server.config.snapshot)
    # A run-token principal (M2B-RUN-TOKENS) may only act on its item: the snapshot is
    # role-agnostic, so it is built as the system principal, as the reaper does.
    from .auth import RunPrincipal
    who = server.system if isinstance(principal, RunPrincipal) else principal
    body = json.dumps(server.store.snapshot(who)).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = getattr(server.config, "signing_key", None)
    if key:   # M2B: signature then snapshot; a reader in the gap retries once. A crash between the
              # two writes leaves a mismatch that FAILS CLOSED until the next write (the reaper
              # rewrites the snapshot every reap_interval), never an unsigned accept.
        _atomic_write(snapshot_sign.sig_path(path), snapshot_sign.sign(body, key).encode())
    _atomic_write(path, body)


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
