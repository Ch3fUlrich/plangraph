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


_COMMANDS = {
    "init": _cmd_init,
    "add-principal": _cmd_add_principal,
    "revoke": _cmd_revoke,
    "verify": _cmd_verify,
    "import": _cmd_import,
    "export": _cmd_export,
}
