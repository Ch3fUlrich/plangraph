"""PG-LOCAL-COPY (D-818): ssh forced-command entry that serves the signed snapshot.

Installed on the keeper host as the ``command=`` of a ``restrict`` authorized_keys
line, so the pull key can do exactly one thing: receive the current
``plan.snapshot.json`` and its detached signature as one JSON line
``{"format", "snapshot" (base64), "sig"}``.  The client's command line
(``SSH_ORIGINAL_COMMAND``) and stdin are ignored; the only input is the config
path in the forced command itself.  A snapshot that does not verify under the
configured ``snapshot_pubkey`` is never served (unsigned keeper = nothing served).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

from . import config as config_mod, snapshot_sign

__all__ = ["FORMAT", "MAX_BYTES", "envelope", "main"]

FORMAT = "plangraph-snapshot-pull-v1"
MAX_BYTES = 64 * 1024 * 1024


def _read(path) -> bytes:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise snapshot_sign.SignError("snapshot larger than the pull limit")
    return data


def envelope(cfg) -> bytes:
    """The one line served: the snapshot bytes and their signature, verified first."""
    pubkey = getattr(cfg, "snapshot_pubkey", None)
    if not pubkey or not getattr(cfg, "signing_key", None):
        raise snapshot_sign.SignError("snapshot signing is not configured; nothing is served")
    path = cfg.snapshot
    for _ in range(2):   # the keeper writes .sig then the snapshot: a racing read retries once
        body = _read(path)
        try:
            sig = _read(snapshot_sign.sig_path(path)).decode("ascii").strip()
        except UnicodeDecodeError:
            sig = ""
        if snapshot_sign.verify(body, sig, pubkey):
            line = {"format": FORMAT, "snapshot": base64.b64encode(body).decode("ascii"),
                    "sig": sig}
            return json.dumps(line, separators=(",", ":")).encode("ascii") + b"\n"
    raise snapshot_sign.SignError("snapshot signature invalid or missing; nothing is served")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot_serve", add_help=False)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)   # the forced command's own argv only
    try:
        cfg = config_mod.load_config(args.config)
        data = envelope(cfg)
    except (OSError, config_mod.ConfigError, snapshot_sign.SignError) as exc:
        print(f"snapshot_serve: {type(exc).__name__}: not served", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
