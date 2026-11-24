"""Definition and output CLI verbs, split from the transport module."""
import json
import os
import sys
from pathlib import Path

from . import fmt
from .brief import creation_error
from .credential_guard import mask_all


def cmd_attempts(args, session):
    from .cli_verbs import read

    return read(session, "attempts", {"since": args.since}, lambda values: "\n".join(
        f"{row['attempt_id']} {row['item']} {row['role']} {row['served_model']}" for row in values))


def cmd_close_merged(args, session):
    from .cli_verbs import SLOW_TIMEOUT, RefusedCli, Unreachable, _refuse
    body = {"key": args.key, "decision_id": args.decision_id, "families": args.families,
            "apply": args.apply, "dry_run": not args.apply, "waive_seats": args.waive_seats}
    if args.from_open:
        body["from_open"] = True
    if args.families is None:
        body.pop("families")  # omission requests automatic recovery
    if not args.from_open:
        return _close_merged_with_read_back(session, args.key, body)
    print("key\tclass\treason")
    try:
        result = session.call("POST", "close_merged", body=body, timeout=SLOW_TIMEOUT)
        if result.get("closed") is not True and not result.get("would_close"):
            print(f"{args.key}\tREFUSED\t{_close_reason(result)}")
            return 2
        classification = "WOULD-CLOSE" if result.get("would_close") else "CLOSED"
        if result.get("flags"):
            classification = "FLAGGED"
        prs = ",".join(f"#{p['pr']}@{p['head']}" for p in result.get("prs", []))
        transition = (f"{result['state']} -> {result['next_state']}; "
                      if result.get("would_close") else "")
        print(f"{args.key}\t{classification}\t{transition}merged PRs with GO and seats {prs}")
        return 0
    except RefusedCli as exc:
        reason = str(exc).replace("\t", " ").replace("\n", " ")
        print(f"{args.key}\t{'NO-WITNESS' if 'NO-WITNESS' in reason else 'REFUSED'}\t{reason}")
        return 2
    except Unreachable as exc:
        if args.apply and _read_back_state(session, args.key) == "done":
            print(f"{args.key}\tCLOSED\t{_read_back_note(exc)}")
            return 0
        return _refuse(exc)


def _close_reason(result):
    reason = result.get("reason") or "close declined"
    missing = result.get("missing") or []
    return reason + (": " + ", ".join(missing) if missing else "")


def _read_back_state(session, key):
    from .cli_verbs import RefusedCli, Unreachable
    try:
        return session.call("GET", "show", {"key": key}).get("state")
    except (RefusedCli, Unreachable):
        return None


def _read_back_note(exc):
    return (f"the client gave up waiting ({exc}); the item reads back done, "
            "closed by this request or a concurrent one")


def _close_merged_with_read_back(session, key, body):
    """POST close_merged; a timeout after a committed close reads the item back and exits 0."""
    from . import outbox
    from .cli_verbs import (
        EXIT_UNREACHABLE,
        SLOW_TIMEOUT,
        RefusedCli,
        Unreachable,
        _emit,
        _refuse,
    )
    try:
        result = session.call("POST", "close_merged", body=body, timeout=SLOW_TIMEOUT)
        if result.get("closed") is False:
            _emit(session, result, lambda r: f"{key} REFUSED {_close_reason(r)}")
            return 2
        return _emit(session, result, lambda r: fmt.result_text("close_merged", r))
    except RefusedCli as exc:
        return _refuse(exc)
    except Unreachable as exc:
        if body["dry_run"] or _read_back_state(session, key) != "done":
            return outbox.on_unreachable(exc, exit_unreachable=EXIT_UNREACHABLE)
        note = _read_back_note(exc)
        print(note, file=sys.stderr)
        return _emit(session, {"key": key, "state": "done", "note": note},
                     lambda r: f"{r['key']} -> done ({r['note']})")


def cmd_legacy(args, session):
    from .cli_verbs import write
    return write(session, "set_legacy_state",
                 {"key": args.key, "state": args.state, "note": args.note},
                 lambda r: f"{r.get('key')} -> {r.get('state')}")


def cmd_priority(args, session):
    from .cli_verbs import write
    return write(session, "set_priority", {"key": args.key, "n": args.n},
                 lambda r: f"{r.get('key')} priority {r.get('priority')}")


def cmd_grant_apply(args, session):
    from .cli_verbs import _refuse, read, write
    if args.list:
        return read(session, "apply_grants", {}, fmt.apply_grants_text)
    if not args.principal or not args.project:
        return _refuse("grant-apply needs PRINCIPAL and PROJECT (or --list)")
    verb = "revoke_apply" if args.revoke else "grant_apply"
    return write(session, verb, {"principal": args.principal, "project": args.project},
                 lambda r: f"{r['principal']} {'revoked' if args.revoke else 'granted'}"
                           f" apply in {r['project']}")


def cmd_define(args, session):
    from .cli_verbs import _refuse, write
    path = Path(args.file)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if args.edit:
        fields = payload.get("fields", payload) if isinstance(payload, dict) else payload
        return write(session, "edit_definition", {"key": args.key, "fields": fields})
    items = payload.get("items") if isinstance(payload, dict) else payload
    if path.name == "slices.json" and isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("slice_parent") is None:
                return _refuse(f"validation: {item.get('project')}:{item.get('id')}: "
                               "slices.json entries require slice_parent")
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


def cmd_categories(args, session):
    from .cli_verbs import read

    return read(session, "categories", {"project": args.project} if args.project else {},
                fmt.categories_text)


def cmd_add_category(args, session):
    from .cli_verbs import write

    return write(session, "add_category", {"name": args.name})


def cmd_rank_categories(args, session):
    from .cli_verbs import write

    return write(session, "rank_categories", {"project": args.project, "order": args.order})


def cmd_backfill_categories(args, session):
    from .cli_verbs import write

    return write(session, "backfill_categories",
                 {"project": args.project, "dry_run": args.dry_run})
