"""Local ``pg admin`` commands: they run on the keeper host against the DB.

``import``/``export`` need :mod:`plangraph.keeper.importer`, which is loaded
lazily inside the handler (through :func:`_importer`) so this module imports even
when only the local-admin commands are used, and so tests can monkeypatch it.
"""
from __future__ import annotations

import os
import sys

from . import auth, config as config_mod, db as kdb
from .errors import Refused
from .store import Store

__all__ = ["dispatch"]


def _importer():
    """Lazy seam for the YAML importer (written separately)."""
    from . import importer
    return importer


def _config(args):
    path = getattr(args, "config", None) or os.environ.get("PG_CONFIG")
    if not path:
        print("admin needs --config or PG_CONFIG", file=sys.stderr)
        return None
    try:
        return config_mod.load_config(path)
    except config_mod.ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return None


def _open(cfg, *, create=False):
    return kdb.open_db(cfg.db, create=create)


def dispatch(args) -> int:
    command = args.admin_command
    cfg = _config(args)
    if cfg is None:
        return 2
    try:
        return _COMMANDS[command](args, cfg)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"admin: {exc}", file=sys.stderr)
        return 2


def _cmd_init(args, cfg) -> int:
    _open(cfg, create=True).close()
    print(f"initialised {cfg.db}")
    if getattr(args, "router", None):
        conn = _open(cfg)
        try:
            token = auth.add_principal(conn, args.router, "router", [])
        finally:
            conn.close()
        print(f"router {args.router} token (shown once): {token}")
    return 0


def _cmd_add_principal(args, cfg) -> int:
    projects = [p for p in (args.projects or "").split(",") if p]
    conn = _open(cfg)
    try:
        token = auth.add_principal(conn, args.name, args.role, projects,
                                   family=args.family)
    finally:
        conn.close()
    print(f"{args.name} token (shown once): {token}")
    return 0


def _cmd_revoke(args, cfg) -> int:
    conn = _open(cfg)
    try:
        auth.revoke(conn, args.name)
    finally:
        conn.close()
    print(f"revoked {args.name}")
    return 0


def _cmd_verify(args, cfg) -> int:
    conn = _open(cfg)
    try:
        store = Store(conn)
        problems = list(store.verify_chain()) + list(store.verify_integrity())
    finally:
        conn.close()
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 2
    print("chain and cache integrity ok")
    return 0


def _cmd_import(args, cfg) -> int:
    items = _importer().load_dir(args.dir)
    conn = _open(cfg)
    try:
        principal = auth.get_principal(conn, args.as_name)
        if principal is None or principal.role != "router":
            print(f"{args.as_name!r} is not a router principal", file=sys.stderr)
            return 2
        result = Store(conn).import_items(principal, items)
    finally:
        conn.close()
    print(f"imported {len(result.get('created') or [])} items")
    return 0


def _cmd_export(args, cfg) -> int:
    conn = _open(cfg)
    try:
        _importer().export_yaml(conn, args.dir)
    finally:
        conn.close()
    print(f"exported to {args.dir}")
    return 0


def _cmd_snapshot_keygen(args, cfg) -> int:
    """M2B: create the snapshot signing key pair (private 0600; never printed)."""
    from . import snapshot_sign
    try:
        snapshot_sign.keygen(args.private, args.public)
    except snapshot_sign.SignError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"key pair written: private {args.private} (0600), public {args.public}")
    return 0


def _cmd_verify_anchor(args, cfg) -> int:
    """M2B-ANCHOR: every anchor record must match the chain; reports the interim +a flag."""
    from . import anchor
    if not anchor.enabled(cfg):
        print("anchors not configured (anchor_socket + anchor_log, or anchor_file)", file=sys.stderr)
        return 2
    path = anchor._log_path(cfg)
    conn = _open(cfg)
    try:
        problems = anchor.verify(conn, path)
        last = anchor.latest(path)
        try:
            anchor.check_start(conn, cfg)
        except anchor.AnchorError as exc:
            problems.append({"code": "anchor_start_refused", "msg": str(exc)})
    finally:
        conn.close()
    for problem in problems:
        print(problem)
    if cfg.get("anchor_file") and not cfg.get("anchor_socket"):
        flag = anchor.append_only(path)
        print("interim anchor file: append-only flag "
              + {True: "set", False: "NOT set (operator: chattr +a)", None: "unknown"}[flag])
    if problems:
        print(f"{len(problems)} anchor problem(s)", file=sys.stderr)
        return 2
    print(f"anchors ok: newest seq {last['seq'] if last else '-'}")
    return 0


def _cmd_verify_snapshot(args, cfg) -> int:
    """M2B: exit 0 iff the snapshot's detached signature verifies under the public key."""
    from . import snapshot_io
    path = args.snapshot or (cfg.snapshot if cfg else None)
    pubkey = args.pubkey or (getattr(cfg, "snapshot_pubkey", None) if cfg else None)
    if not path or not pubkey:
        print("need --snapshot and --pubkey (or snapshot / snapshot_pubkey in the config)",
              file=sys.stderr)
        return 2
    try:
        snapshot_io.read_snapshot(path, pubkey)
    except (OSError, snapshot_io.SignError) as exc:
        print(f"snapshot NOT verified: {exc}", file=sys.stderr)
        return 1
    print("snapshot signature ok")
    return 0


_COMMANDS = {
    "snapshot-keygen": _cmd_snapshot_keygen,
    "verify-snapshot": _cmd_verify_snapshot,
    "verify-anchor": _cmd_verify_anchor,
    "init": _cmd_init,
    "add-principal": _cmd_add_principal,
    "revoke": _cmd_revoke,
    "verify": _cmd_verify,
    "import": _cmd_import,
    "export": _cmd_export,
}
