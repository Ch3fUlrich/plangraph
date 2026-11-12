"""Definition and output CLI verbs, split from the transport module."""
import json
import os
import sys
from pathlib import Path

from . import fmt
from .brief import creation_error
from .credential_guard import mask_all


def cmd_close_merged(args, session):
    from .cli_verbs import write
    return write(session, "close_merged", {"key": args.key,
                 "decision_id": args.decision_id, "families": args.families})


def cmd_legacy(args, session):
    from .cli_verbs import write
    return write(session, "set_legacy_state",
                 {"key": args.key, "state": args.state, "note": args.note},
                 lambda r: f"{r.get('key')} -> {r.get('state')}")


def cmd_priority(args, session):
    from .cli_verbs import write
    return write(session, "set_priority", {"key": args.key, "n": args.n},
                 lambda r: f"{r.get('key')} priority {r.get('priority')}")


def cmd_define(args, session):
    from .cli_verbs import _refuse, write
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if args.edit:
        fields = payload.get("fields", payload) if isinstance(payload, dict) else payload
        return write(session, "edit_definition", {"key": args.key, "fields": fields})
    items = payload.get("items") if isinstance(payload, dict) else payload
    if error := creation_error(items):
        return _refuse(error)
    if tier := getattr(args, "tier", None):
        for definition in (items if isinstance(items, list) else [items]):
            if isinstance(definition, dict):
                definition["review_tier"] = tier
    return write(session, "create_items", {"items": items},
                  lambda r: fmt.result_text("create_items", r))


def cmd_render(args, session):
    from .. import render as render_mod
    from .cli_verbs import (
        EXIT_OK,
        EXIT_UNREACHABLE,
        NOTICE,
        RefusedCli,
        Unreachable,
        _refuse,
    )
    try:
        snapshot = session.call("GET", "snapshot")
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable:
        print(NOTICE, file=sys.stderr)
        try:
            snapshot = session.local_snapshot()
        except Unreachable as exc:
            print(f"keeper unreachable: {exc}", file=sys.stderr)
            return EXIT_UNREACHABLE
    snapshot, masked = mask_all(snapshot)  # D-374: last line of defence
    if masked:
        print(f"render: masked {masked} credential-shaped string(s)",
              file=sys.stderr)
    document = render_mod.render(snapshot, render_mod.default_vendor_dir(), cdn=args.cdn,
                                 live_agents=os.environ.get("PLANGRAPH_LIVE_AGENTS") == "1")
    out = Path(args.out)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")
    from .. import (
        kg_export,  # PG-KG-EXPORT: read-only, off without a token, never fails the render
    )
    kg_export.run(snapshot.get("items") or [], out.parent)
    return EXIT_OK
