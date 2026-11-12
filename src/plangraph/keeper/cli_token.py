"""``pg token KEY --role R --run RUN`` (M2B-RUN-TOKENS): mint a run token.

Router or the item's l1 only.  The token goes to stdout and nowhere else (not
argv, not a log, not ``--json`` metadata on stderr); the spawner hands it to
the run through its environment or an inherited fd.
"""
from __future__ import annotations

import sys

from .cli_verbs import EXIT_OK, EXIT_REFUSED, EXIT_UNREACHABLE, RefusedCli, Unreachable


def cmd_token(args, session) -> int:
    host = getattr(args, 'host', None)
    revoke = getattr(args, 'revoke_host', None)
    verb = 'mint_host_token' if host else 'revoke_host_token' if revoke else 'mint_run_token'
    body = ({'host_id': host} if host else {'token_id': revoke} if revoke else
            {"key": args.key, "role": args.role, "run_id": args.run})
    if not host and not revoke and not all((args.key, args.role, args.run)):
        print('run token requires key, role and run', file=sys.stderr)
        return EXIT_REFUSED
    try:
        out = session.call("POST", verb, body=body)
    except RefusedCli as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except Unreachable as exc:
        print(f"keeper unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    if revoke:
        return EXIT_OK
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
    p.add_argument("key", nargs='?')
    p.add_argument("--role", choices=("writer", "reviewer"))
    p.add_argument("--run", help="autoos run id (must exist)")
    group = p.add_mutually_exclusive_group()
    group.add_argument('--host', help='router/l1: mint ingest-only host token')
    group.add_argument('--revoke-host', type=int, help='router/l1: revoke host token id')
