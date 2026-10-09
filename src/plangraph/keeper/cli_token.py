"""``pg token KEY --role R --run RUN`` (M2B-RUN-TOKENS): mint a run token.

Router or the item's l1 only.  The token goes to stdout and nowhere else (not
argv, not a log, not ``--json`` metadata on stderr); the spawner hands it to
the run through its environment or an inherited fd.
"""
from __future__ import annotations

import sys

from .cli_verbs import EXIT_OK, EXIT_REFUSED, EXIT_UNREACHABLE, RefusedCli, Unreachable


def cmd_token(args, session) -> int:
    body = {"key": args.key, "role": args.role, "run_id": args.run}
    try:
        out = session.call("POST", "mint_run_token", body=body)
    except RefusedCli as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except Unreachable as exc:
        print(f"keeper unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    token = out.get("token")
    if not token:
        print("keeper returned no token", file=sys.stderr)
        return EXIT_REFUSED
    print(token)
    print(f"{out.get('key')} {out.get('role')} epoch {out.get('epoch')} "
          f"expires {out.get('expires_at')}", file=sys.stderr)
    return EXIT_OK


def add_parser(sub, make):
    p = make(sub, "token", help="router/l1: mint a run-bound token (printed once)")
    p.add_argument("key")
    p.add_argument("--role", required=True, choices=("writer", "reviewer"))
    p.add_argument("--run", required=True, help="autoos run id (must exist)")
