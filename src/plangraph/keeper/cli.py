"""``pg`` — the keeper command line client.

``python -m plangraph.keeper.cli <verb> [args]`` talks to the API named by
``PG_URL`` with the token from ``PG_TOKEN_FILE``.  ``pg admin …`` runs locally
on the keeper host.  Exit codes: 0 ok, 2 refused, 3 unreachable.
"""
from __future__ import annotations

import argparse
import sys

from . import cli_admin, cli_verbs
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
    "priority": cli_verbs.cmd_priority,
    "define": cli_verbs.cmd_define,
    "blocked": cli_verbs.cmd_blocked,
    "waves": cli_verbs.cmd_waves,
    "critical": cli_verbs.cmd_critical,
    "percent": cli_verbs.cmd_percent,
    "alerts": cli_verbs.cmd_alerts,
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

    show = _p(sub, "show", help="one item, compact")
    show.add_argument("key")

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
    evidence.add_argument("--decision")

    verdict = _p(sub, "verdict", help="pass or send back a review")
    verdict.add_argument("key")
    verdict.add_argument("--pass", dest="ok", action="store_true")
    verdict.add_argument("--changes", metavar="WHY")
    verdict.add_argument("--reason")

    cancel = _p(sub, "cancel", help="cancel an item")
    cancel.add_argument("key")
    cancel.add_argument("--reason")

    priority = _p(sub, "priority", help="set an item's priority")
    priority.add_argument("key")
    priority.add_argument("n", type=int)

    define = _p(sub, "define", help="create or edit definitions from JSON")
    define.add_argument("key", nargs="?")
    define.add_argument("--file", required=True)
    define.add_argument("--edit", action="store_true")

    render = _p(sub, "render", help="render the snapshot to HTML")
    render.add_argument("--out", required=True)
    render.add_argument("--cdn", action="store_true")


def _add_admin(sub):
    admin = sub.add_parser("admin", help="local commands on the keeper host")
    commands = admin.add_subparsers(dest="admin_command", required=True)

    init = commands.add_parser("init", help="create the DB schema")
    init.add_argument("--router", help="also create a router principal")

    add = commands.add_parser("add-principal")
    add.add_argument("name")
    add.add_argument("--role", required=True)
    add.add_argument("--projects", default="")
    add.add_argument("--family", default=None)

    revoke = commands.add_parser("revoke")
    revoke.add_argument("name")

    commands.add_parser("verify", help="verify the hash chain and cache")

    imp = commands.add_parser("import")
    imp.add_argument("dir")
    imp.add_argument("--as", dest="as_name", required=True)

    export = commands.add_parser("export")
    export.add_argument("dir")


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
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "admin":
        return cli_admin.dispatch(args)
    session = Session(json_mode=args.json, config_path=args.config)
    return HANDLERS[args.command](args, session)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
