"""Keeper CLI adapter for the native durable run queue."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

QUEUE_VERBS = frozenset({"heartbeat", "add_evidence", "submit"})
EXIT_QUEUED = 4


def _native(args, body=None):
    from .cli_verbs import Unreachable

    try:
        result = subprocess.run(
            ["plangraph", "keeper-outbox", *args],
            input=None if body is None else json.dumps(body),
            text=True, capture_output=True, check=False)
    except OSError as exc:
        raise Unreachable(f"cannot start the native keeper outbox: {exc}") from exc
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        return result.returncode, None
    try:
        return 0, json.loads(result.stdout)
    except ValueError as exc:
        raise Unreachable("native keeper outbox returned malformed output") from exc


def write(session, verb, body):
    """Send the explicit receipt verbs through the same queue as replay."""
    from .cli_verbs import RefusedCli

    code, outcome = _native(["write", session.url, verb], body)
    if code:
        return code, None
    result = outcome.get("first") if outcome.get("result") == "duplicate" else outcome.get("result")
    if result != "ok":
        raise RefusedCli(result, outcome.get("msg", ""))
    return 0, outcome["reply"]


def queue_http_heartbeat(verb, body, request_key):
    """Write a keyed HTTP heartbeat in the legacy format the native queue imports."""
    path = os.environ.get("PG_OUTBOX")
    if verb != "heartbeat" or not path:
        return False
    entry = {"id": uuid.uuid4().hex, "verb": verb, "body": body,
             "idempotency_key": request_key}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    print(f"keeper unreachable: queued {verb} in the outbox ({entry['id']})", file=sys.stderr)
    return True


def on_unreachable(exc, *, exit_unreachable=3):
    print(f"keeper unreachable: {exc}", file=sys.stderr)
    return exit_unreachable


def cmd_replay(args, session):
    from .cli_verbs import EXIT_REFUSED, EXIT_UNREACHABLE, RefusedCli, Unreachable

    try:
        if args.resolve_entry:
            if not args.decision or not os.environ.get("PG_OUTBOX"):
                raise RefusedCli("resolution requires --decision and the run's PG_OUTBOX")
            actor = session.call("GET", "whoami")
            if actor.get("role") != "router":
                raise RefusedCli("only the router may resolve a queued dependency")
            code, _ = _native(["resolve", args.resolve_entry, args.decision])
            return code
        code, outcomes = _native(["replay", session.url])
        for outcome in outcomes or []:
            print(f"{outcome['id']} {outcome['result']}")
        return code
    except RefusedCli as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except Unreachable as exc:
        print(exc, file=sys.stderr)
        return EXIT_UNREACHABLE


def add_parser(sub, make):
    parser = make(sub, "replay", help="deliver the run's durable keeper writes in order")
    parser.add_argument("--resolve-entry", metavar="INVOCATION")
    parser.add_argument("--decision", metavar="D-ID")
