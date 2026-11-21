"""Local ``pg admin`` commands: they run on the keeper host against the DB.

``import``/``export`` need :mod:`plangraph.keeper.importer`, which is loaded
lazily inside the handler (through :func:`_importer`) so this module imports even
when only the local-admin commands are used, and so tests can monkeypatch it.
"""
from __future__ import annotations

import os
import stat
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
    return kdb.open_db(cfg.db, create=create, **cfg.db_options)


def _cmd_prune_backups(args) -> int:
    """PG-BACKUP-RETENTION: needs no config; --dir is explicit; dry-run unless --apply."""
    from . import backup_retention
    try:
        res = backup_retention.prune(args.dir, args.keep, args.daily_days,
                                     apply=bool(args.apply))
    except (OSError, ValueError) as exc:
        print(f"prune-backups: {exc}", file=sys.stderr)
        return 2
    return 0 if res.ok else 1


def _write_new_private(target, text: str) -> None:
    """Atomically create *target* with mode 0600 if it does not already exist."""
    import tempfile
    from pathlib import Path

    path = Path(target)
    if os.path.lexists(path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_sweep_config(*, example, config_dir, state_dir, token_file, env_file,
                         pins_file, repo_root=None) -> None:
    """Create disk-hygiene.env and pins.json from example when token_file is a regular file."""
    from pathlib import Path

    token_path = Path(token_file)
    if token_path.is_symlink() or not token_path.is_file():
        raise ValueError("sweep token file must be a regular file, not a symlink")
    example_path = Path(example)
    if not example_path.is_file():
        raise ValueError(f"missing {example_path}")
    cfg_dir = Path(config_dir)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    home = Path(os.path.abspath(os.environ["HOME"]))
    root = Path(repo_root).resolve() if repo_root else example_path.resolve().parents[2]
    env_path = Path(env_file)
    if not os.path.lexists(env_path):
        default_repo = home / "code/plangraph"
        if (default_repo / ".venv/bin/python3").is_file():
            reader_src = str(default_repo / "src")
            reader_py = str(default_repo / ".venv/bin/python3")
        elif (root / ".venv/bin/python3").is_file():
            reader_src = str(root / "src")
            reader_py = str(root / ".venv/bin/python3")
        else:
            reader_src = str(root / "src")
            reader_py = sys.executable
        pg_url = os.environ.get("PG_URL", "").strip()
        if not pg_url:
            pg_env = cfg_dir / "plangraph.env"
            if pg_env.is_file() and not pg_env.is_symlink():
                for line in pg_env.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    if k.strip() == "PG_URL":
                        pg_url = v.strip().strip("\"'")
                        break
        text = example_path.read_text(encoding="utf-8")
        text = text.replace(
            "PYTHONPATH=/home/USER/code/plangraph/src /home/USER/code/plangraph/.venv/bin/python3",
            f"PYTHONPATH={reader_src} {reader_py}",
        )
        text = text.replace("/home/USER/.config/plangraph/pins.json", str(pins_file))
        text = text.replace("/home/USER/.config/plangraph/sweep.token", str(token_file))
        text = text.replace("/home/USER/.local/state/plangraph", str(state_dir))
        text = text.replace("/home/USER", str(home))
        if pg_url:
            text = text.replace("PG_URL=https://KEEPER_HOST", f"PG_URL={pg_url}")
        _write_new_private(env_path, text)
    _write_new_private(pins_file, "[]\n")


def _cmd_install_sweep(args) -> int:
    try:
        install_sweep_config(
            example=args.example,
            config_dir=args.config_dir,
            state_dir=args.state_dir,
            token_file=args.token_file,
            env_file=args.env_file,
            pins_file=args.pins_file,
            repo_root=args.repo_root,
        )
    except (OSError, ValueError) as exc:
        print(f"worktree-sweep: {exc}", file=sys.stderr)
        return 2
    return 0


def dispatch(args) -> int:
    command = args.admin_command
    if command == "prune-backups":     # before _config(): no keeper config needed
        return _cmd_prune_backups(args)
    if command == "install-sweep":     # before _config(): bootstrap runs without keeper config
        return _cmd_install_sweep(args)
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


def _cmd_migrate(args, cfg) -> int:
    """Bring the schema to the current version (Postgres runs the pending migrations)."""
    postgres = kdb.select_backend(cfg.db_options.get("backend")) == "postgres"
    if args.dsn_file:
        if not postgres:
            raise ValueError("a migration DSN requires the Postgres backend")
        opts = {**cfg.db_options, "dsn": _migration_dsn(args.dsn_file)}
        kdb.open_db(cfg.db, create=True, **opts).close()
    else:
        _open(cfg, create=postgres).close()  # SQLite upgrades on open
    print("schema up to date")
    return 0


def _migration_dsn(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())):
            raise ValueError("migration DSN must be a regular file owned by the caller with mode 0600")
        try:
            dsn = file.read(16385).strip()
        except UnicodeError:
            raise ValueError("migration DSN must be UTF-8") from None
    if not dsn or len(dsn) > 16384:
        raise ValueError("migration DSN must be nonempty and at most 16384 characters")
    return dsn


def _cmd_check_schema(_args, cfg) -> int:
    if kdb.select_backend(cfg.db_options.get("backend")) != "postgres":
        raise ValueError("check-schema requires the Postgres backend")
    _open(cfg).close()
    print("runtime schema verified")
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


def _cmd_mint_oversight_token(args, cfg) -> int:
    """R2: the only door.  Plaintext goes to a 0600 file, never to stdout.

    build-L1 LOW: write the new plaintext file first (0600, atomic rename),
    THEN revoke the old tokens. If the write fails, the old tokens are kept.
    """
    import tempfile
    conn = _open(cfg)
    try:
        store = Store(conn)
        # Phase 1: validate and generate token WITHOUT DB changes
        prepared = store.prepare_oversight_token(
            args.name, go_decision=args.go, reason=args.reason)
    finally:
        conn.close()
    path = args.token_out
    # Write to a temporary file first, then atomic rename (build-L1 LOW)
    dir_name = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".token-", suffix=".tmp")
    try:
        try:
            os.write(fd, (prepared["token"] + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        # Phase 2: ONLY AFTER successful file write, do the DB operations
        conn = _open(cfg)
        try:
            Store(conn).commit_oversight_token(prepared)
        finally:
            conn.close()
        print(f"oversight token written to {path}")
        return 0
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _problem_line(problem) -> str:
    """One line per integrity problem; unknown types name the type and seq.

    Every other problem keeps its plain dict repr (the contract greps the
    codes out of it), but an ``unknown_event_type`` carries no expected/got
    pair, so it reads as a sentence instead.
    """
    if problem.get("code") == "unknown_event_type":
        return (f"{problem.get('key') or '-'} unknown_event_type"
                f" type={problem.get('got')} seq={problem.get('seq')}")
    return str(problem)


def _cmd_verify(args, cfg) -> int:
    conn = _open(cfg)
    try:
        store = Store(conn)
        problems = list(store.verify_chain()) + list(store.verify_integrity())
    finally:
        conn.close()
    for problem in problems:
        print(_problem_line(problem))
    if problems:
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 2
    print("chain and cache integrity ok")
    return 0


def _cmd_copy_to_pg(args, cfg) -> int:
    """W3: SQLite snapshot -> empty Postgres keeper (``--dry-run`` reads the source only)."""
    from . import copy_to_pg
    return copy_to_pg.run(cfg, src=args.src, apply=args.apply)


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
    "migrate": _cmd_migrate,
    "check-schema": _cmd_check_schema,
    "add-principal": _cmd_add_principal,
    "revoke": _cmd_revoke,
    "mint-oversight-token": _cmd_mint_oversight_token,
    "verify": _cmd_verify,
    "copy-to-pg": _cmd_copy_to_pg,
    "import": _cmd_import,
    "export": _cmd_export,
}
