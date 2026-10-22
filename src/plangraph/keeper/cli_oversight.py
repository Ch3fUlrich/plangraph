"""``oversight-check`` (RT-OVERSIGHT-CHECKS): the local oversight pass.

Runs :func:`plangraph.keeper.oversight.run_checks` over the configured keeper
DB with the real clock (and the config's repos as the config-style ``git``
when it declares any), then writes ``DIR/digest.json`` and ``DIR/digest.sha256``
atomically (temp file + rename, the snapshot-writer shape) and raises
``projector_alarms`` rows for the findings (``--no-alarms`` skips that half).

Exit 0 even with findings; nonzero (2) only on a config/runtime failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from . import config as config_mod, db as kdb
from .cli_verbs import EXIT_OK, EXIT_REFUSED

__all__ = ["cmd_oversight_check", "add_parser"]


def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* through a temp file in the same directory."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
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


def _config(session):
    """The session config, or ``None`` with a one-line reason on stderr."""
    try:
        cfg = session.config
    except config_mod.ConfigError as exc:
        print(f"oversight-check config: {exc}", file=sys.stderr)
        return None
    if cfg is None:
        print("oversight-check needs --config or PG_CONFIG", file=sys.stderr)
    return cfg


def cmd_oversight_check(args, session):
    from . import oversight     # lazy: oversight.py ships in its own lane
    cfg = _config(session)
    if cfg is None:
        return EXIT_REFUSED
    try:
        out = Path(getattr(args, "out_dir", None) or ".")
        out.mkdir(parents=True, exist_ok=True)
        conn = kdb.open_db(cfg.db, **cfg.db_options)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"oversight-check: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        now = time.time()
        # Config-style git (repos mapping): stall's r1/r2 read it, and a rule
        # that wants an executable git simply skips its findings without one.
        findings = oversight.run_checks(conn, now,
                                        git=cfg if cfg.repos else None)
        text, sha = oversight.digest(findings)
        _atomic_write(out / "digest.json", text.encode("ascii"))
        _atomic_write(out / "digest.sha256",
                      f"{sha}  digest.json\n".encode("ascii"))
        if getattr(args, "no_alarms", False):
            new = 0
        else:
            new = oversight.raise_alarms(conn, findings, now)
    except Exception as exc:
        print(f"oversight-check: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        conn.close()
    print(f"oversight: {len(findings)} findings, {new} new alarms, sha256 {sha}")
    return EXIT_OK


def add_parser(sub, make):
    p = make(sub, "oversight-check",
             help="RT-OVERSIGHT-CHECKS: run the checks, write the digest, raise alarms")
    p.add_argument("--out-dir", default=".", metavar="DIR",
                   help="write DIR/digest.json and DIR/digest.sha256 (default .)")
    p.add_argument("--no-alarms", action="store_true",
                   help="write the digest but leave projector_alarms alone")
