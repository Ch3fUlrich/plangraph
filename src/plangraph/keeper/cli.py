"""``pg`` — the keeper command line client.

``python -m plangraph.keeper.cli <verb> [args]`` talks to the API named by
``PG_URL`` with the token from ``PG_TOKEN_FILE``.  ``pg admin …`` runs locally
on the keeper host.  Exit codes: 0 ok, 2 refused, 3 unreachable.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import (cli_admin, cli_audit_meta, cli_changelog, cli_names,
               cli_progress, cli_token, cli_verbs, outbox)
from .cli_verbs import Session

__all__ = ["main", "build_parser"]

_COMMON = argparse.ArgumentParser(add_help=False)
_COMMON.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                     help="full JSON output instead of compact text")


def _p(sub, name, **kw):
    return sub.add_parser(name, parents=[_COMMON], **kw)


HANDLERS = {
    "ready": cli_verbs.cmd_ready,
    "show": cli_verbs.cmd_show,
    "claim": cli_verbs.cmd_claim,
    "delegate": cli_verbs.cmd_delegate,
    "heartbeat": cli_verbs.cmd_heartbeat,
    "release": cli_verbs.cmd_release,
    "evidence": cli_verbs.cmd_evidence,
    "submit": cli_verbs.cmd_submit,
    "verdict": cli_verbs.cmd_verdict,
    "cancel": cli_verbs.cmd_cancel,
    "waive": cli_verbs.cmd_waive,
    "withdraw": cli_verbs.cmd_withdraw,
    "set-kind": cli_verbs.cmd_set_kind,
    "close": cli_verbs.cmd_close,
    "legacy": cli_verbs.cmd_legacy,
    "priority": cli_verbs.cmd_priority,
    "migrate-descriptions": cli_names.cmd_migrate_descriptions,
    "rehome": cli_names.cmd_rehome,
    "aliases": cli_names.cmd_aliases,
    "define": cli_verbs.cmd_define,
    "blocked": cli_verbs.cmd_blocked,
    "waves": cli_verbs.cmd_waves,
    "critical": cli_verbs.cmd_critical,
    "percent": cli_verbs.cmd_percent,
    "alerts": cli_verbs.cmd_alerts,
    "progress": cli_progress.cmd_progress,
    "changelog": cli_changelog.cmd_changelog,
    "audit-meta": cli_audit_meta.cmd_audit_meta,
    "replay": outbox.cmd_replay,
    "token": cli_token.cmd_token,
    "digest": cli_verbs.cmd_digest,
    "snapshot": cli_verbs.cmd_snapshot,
    "whoami": cli_verbs.cmd_whoami,
    "render": cli_verbs.cmd_render,
}


def _add_reads(sub):
    ready = _p(sub, "ready", help="items ready to work now")
    ready.add_argument("--project")
    ready.add_argument("--all", action="store_true")
    ready.add_argument("-n", type=int, default=10)
    ready.add_argument("--brief", action="store_true",
                       help="one compact line per item (the default; beats --json)")
    ready.add_argument("--owner", help="only items owned by this principal")
    ready.add_argument("--mine", action="store_true", help="only items owned by you (D-953)")
    ready.add_argument("--agent", action="store_true",
                       help="drop operator steps (operator flag or question/decision/operator_task)")

    show = _p(sub, "show", help="one item, compact")
    show.add_argument("key")
    show.add_argument("--brief", action="store_true",
                      help="short task brief instead of the full item")

    blocked = _p(sub, "blocked", help="open items and their unmet deps")
    blocked.add_argument("--project")

    _p(sub, "waves", help="topological layers")

    critical = _p(sub, "critical", help="longest remaining path")
    critical.add_argument("--project")

    percent = _p(sub, "percent", help="percent done")
    percent.add_argument("--project")
    percent.add_argument("--by", choices=("estimate", "count"), default="estimate")

    alerts = _p(sub, "alerts", help="leases, overdue reviews, findings")
    alerts.add_argument("--project")

    _p(sub, "digest", help="per-project counts (router)")
    cli_progress.add_parser(sub, _p)
    cli_changelog.add_parser(sub, _p)
    cli_audit_meta.add_parser(sub, _p)
    outbox.add_parser(sub, _p)
    cli_token.add_parser(sub, _p)
    _p(sub, "snapshot", help="the plan snapshot")
    _p(sub, "whoami", help="the token's principal")


def _add_writes(sub):
    claim = _p(sub, "claim", help="claim a ready item")
    claim.add_argument("key")
    claim.add_argument("--lease", help="lease, e.g. 2h")

    delegate = _p(sub, "delegate", help="delegate a claimed item")
    delegate.add_argument("key")
    delegate.add_argument("worker")

    for name in ("heartbeat", "release", "submit"):
        parser = _p(sub, name)
        parser.add_argument("key")

    evidence = _p(sub, "evidence", help="attach evidence")
    evidence.add_argument("key")
    evidence.add_argument("--kind", required=True)
    evidence.add_argument("--commit")
    evidence.add_argument("--run")
    evidence.add_argument("--task")
    evidence.add_argument("--project", help="semaphore project id (semaphore-task)")
    evidence.add_argument("--template", help="semaphore template id (semaphore-task)")
    evidence.add_argument("--decision")

    verdict = _p(sub, "verdict", help="pass or send back a review")
    verdict.add_argument("key")
    verdict.add_argument("--pass", dest="ok", action="store_true")
    verdict.add_argument("--changes", metavar="WHY")
    verdict.add_argument("--reason")

    cancel = _p(sub, "cancel", help="cancel an item")
    cancel.add_argument("key")
    cancel.add_argument("--reason")

    waive = _p(sub, "waive", help="router: waive the second reviewer family")
    waive.add_argument("key")
    waive.add_argument("decision_id", metavar="D-NNN")

    withdraw = _p(sub, "withdraw", help="router: take a decision-closable item out of review")
    withdraw.add_argument("key")
    withdraw.add_argument("decision_id", metavar="D-NNN")

    set_kind = _p(sub, "set-kind", help="router: turn an open lane into a question / operator_task")
    set_kind.add_argument("key")
    set_kind.add_argument("kind", help="question or operator_task (the keeper checks)")

    close = _p(sub, "close", help="router: close a tier-none item on a decision batch")
    close.add_argument("key")
    close.add_argument("--batch", required=True, metavar="PATH@SHA",
                       help="batch file path@sha256 inside a batch_roots dir")

    legacy = _p(sub, "legacy", help="router: set an open legacy item's state")
    legacy.add_argument("key")
    legacy.add_argument("state", choices=("claimed", "in_review"))
    legacy.add_argument("note", nargs="?", default="")

    _p(sub, "aliases", help="list old -> new keys left by rehome")
    _p(sub, "migrate-descriptions",
       help="router: move folded 'description:' note lines into the column")

    rehome = _p(sub, "rehome", help="router: move an open item to another project")
    rehome.add_argument("key")
    rehome.add_argument("project", help="the new project")
    rehome.add_argument("--host", default=None, help="host of the moved item")

    priority = _p(sub, "priority", help="set an item's priority")
    priority.add_argument("key")
    priority.add_argument("n", type=int)

    define = _p(sub, "define", help="create or edit definitions from JSON")
    define.add_argument("key", nargs="?")
    define.add_argument("--file", required=True)
    define.add_argument("--edit", action="store_true")
    define.add_argument("--tier", choices=("standard", "high", "none"),
                        default=None, help="override review_tier on create")

    render = _p(sub, "render", help="render the snapshot to HTML")
    render.add_argument("--out", required=True)
    render.add_argument("--cdn", action="store_true")


def _add_admin(sub):
    admin = sub.add_parser("admin", help="local commands on the keeper host")
    commands = admin.add_subparsers(dest="admin_command", required=True)

    init = commands.add_parser("init", help="create the DB schema")
    init.add_argument("--router", help="also create a router principal")

    commands.add_parser("migrate", help="apply pending schema migrations (Postgres) / upgrade (SQLite)")

    add = commands.add_parser("add-principal")
    add.add_argument("name")
    add.add_argument("--role", required=True)
    add.add_argument("--projects", default="")
    add.add_argument("--family", default=None)

    revoke = commands.add_parser("revoke")
    revoke.add_argument("name")

    commands.add_parser("verify", help="verify the hash chain and cache")

    copy = commands.add_parser("copy-to-pg", help="W3: copy a SQLite keeper snapshot into an empty Postgres keeper")
    mode = copy.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="read the source only: counts, chain head, chain check")
    mode.add_argument("--apply", action="store_true",
                      help="copy in one transaction, verify, else roll back")
    copy.add_argument("--src", default=None, help="SQLite keeper file (default: the configured db)")

    imp = commands.add_parser("import")
    imp.add_argument("dir")
    imp.add_argument("--as", dest="as_name", required=True)

    export = commands.add_parser("export")
    export.add_argument("dir")

    keygen = commands.add_parser("snapshot-keygen", help="M2B: snapshot signing key pair")
    keygen.add_argument("--private", required=True)
    keygen.add_argument("--public", required=True)
    vsnap = commands.add_parser("verify-snapshot", help="M2B: check the snapshot signature")
    vsnap.add_argument("--snapshot")
    vsnap.add_argument("--pubkey")
    commands.add_parser("verify-anchor", help="M2B: check the anchor log against the chain")

    prune = commands.add_parser(
        "prune-backups", help="retention for ad-hoc backup files in a data dir (dry run by default)")
    prune.add_argument("--dir", required=True, help="the keeper data dir (no default)")
    prune.add_argument("--keep", type=int, default=10, help="newest files to keep (default 10)")
    prune.add_argument("--daily-days", dest="daily_days", type=int, default=7,
                       help="also keep the newest file of each of the last N UTC days (default 7)")
    prune.add_argument("--apply", action="store_true",
                       help="actually delete (absent = dry run)")


def build_parser():
    parser = argparse.ArgumentParser(prog="pg", description="Keeper client.")
    parser.add_argument("--json", action="store_true",
                        help="full JSON output instead of compact text")
    parser.add_argument("--config", default=None,
                        help="config path for admin/fallback (or PG_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_reads(sub)
    _add_writes(sub)
    _add_admin(sub)
    sync = sub.add_parser("sync", help="D-818: refresh this host's signed local plan copy")
    sync.add_argument("sync_command", choices=["pull"])
    return parser


def _cmd_sync(args) -> int:
    """``pg sync pull``: 0 installed, 1 refused, 3 keeper unreachable (copy unchanged)."""
    from . import config as config_mod, snapshot_pull
    path = args.config or os.environ.get("PG_CONFIG")
    if not path:
        print("pg sync needs --config or PG_CONFIG (the reader config)", file=sys.stderr)
        return 2
    try:
        print(snapshot_pull.pull(config_mod.load_config(path)))
    except snapshot_pull.Unreachable as exc:
        print(f"unreachable: {exc}; local copy unchanged", file=sys.stderr)
        return 3
    except (OSError, config_mod.ConfigError, snapshot_pull.PullError,
            snapshot_pull.snapshot_sign.SignError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "admin":
        return cli_admin.dispatch(args)
    if args.command == "sync":
        return _cmd_sync(args)
    session = Session(json_mode=args.json, config_path=args.config)
    return HANDLERS[args.command](args, session)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
