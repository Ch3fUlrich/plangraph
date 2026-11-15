"""Definition and output CLI verbs, split from the transport module."""
import json
import os
import sys
from pathlib import Path

from . import fmt
from .brief import creation_error
from .credential_guard import mask_all


def cmd_close_merged(args, session):
    from .cli_verbs import write, RefusedCli, Unreachable, _refuse
    body = {"key": args.key, "decision_id": args.decision_id, "families": args.families,
            "from_open": args.from_open, "apply": args.apply, "waive_seats": args.waive_seats}
    if args.families is None:
        body.pop("families")  # omission requests automatic recovery
    if not args.from_open:
        return write(session, "close_merged", body)
    print("key\tclass\treason")
    try:
        result = session.call("POST", "close_merged", body=body)
        classification = "WOULD-CLOSE" if result.get("would_close") else "CLOSED"
        prs = ",".join(f"#{p['pr']}@{p['head']}" for p in result.get("prs", []))
        print(f"{args.key}\t{classification}\tmerged PRs with GO and seats {prs}")
        return 0
    except RefusedCli as exc:
        reason = str(exc).replace("\t", " ").replace("\n", " ")
        print(f"{args.key}\t{'NO-WITNESS' if 'NO-WITNESS' in reason else 'REFUSED'}\t{reason}")
        return 2
    except Unreachable as exc:
        return _refuse(exc)


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
    # The store checks decisions after duplicate/conflict/lock refusals.
    checked = [item for item in items if isinstance(item, dict)
               and item.get("kind") != "decision"] if isinstance(items, list) else []
    if error := creation_error(checked):
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
