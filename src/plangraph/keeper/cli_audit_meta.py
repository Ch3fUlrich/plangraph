"""``pg audit-meta`` (PG-AUDIT-META): missing metadata and stale claims.

``--skeleton DIR`` is the CLI half of the read: the verb only returns data, this
writes one fill-in-the-blanks batch file per project beside its note file.
"""
from __future__ import annotations

import sys

from . import audit_meta
from .cli_verbs import (EXIT_REFUSED, EXIT_UNREACHABLE, RefusedCli, Unreachable,
                        _emit, _refuse, read)


def _skeleton(args, data) -> None:
    for path in audit_meta.write_skeleton(data, args.skeleton):
        print(str(path), file=sys.stderr)


def cmd_audit_meta(args, session):
    params = {"project": args.project, "stale_days": args.stale_days}
    if not args.skeleton:
        return read(session, "audit-meta", params, audit_meta.render)
    # the files need the data even under --json, so emit here rather than
    # through ``read``'s renderer (which only runs on the text path).
    try:
        data = session.call("GET", "audit-meta", params)
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable as exc:
        print(f"keeper unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    code = _emit(session, data, audit_meta.render)
    try:
        _skeleton(args, data)
    except OSError as exc:
        print(f"skeleton: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return code


def add_parser(sub, make):
    p = make(sub, "audit-meta",
             help="items missing description/estimate/criteria/owner, stale claims")
    p.add_argument("--project", help="audit one project (router: any, L1: its own)")
    p.add_argument("--stale-days", type=int, default=audit_meta.DEFAULT_STALE_DAYS,
                   metavar="N", help="a claim is stale after N quiet days (default 3)")
    p.add_argument("--skeleton", metavar="DIR",
                   help="write DIR/<project>.BATCH.yaml stubs for what is missing")
