"""``pg`` commands for the item-naming verbs: migrate-descriptions, rehome, aliases."""
from __future__ import annotations

from . import fmt
from .cli_verbs import read, write


def cmd_migrate_descriptions(args, session):
    return write(session, "migrate_descriptions", {},
                 lambda r: f"migrated {len(r['migrated'])}, "
                           f"skipped {len(r['skipped'])}")


def cmd_rehome(args, session):
    body = {"key": args.key, "new_project": args.project}
    if args.host is not None:
        body["host"] = args.host
    return write(session, "rehome", body,
                 lambda r: f"{r['old']} -> {r['new']} host {r.get('host') or '-'}")


def cmd_aliases(args, session):
    return read(session, "aliases", {}, fmt.aliases_text)
