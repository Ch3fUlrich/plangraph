"""M2B-OUTBOX client: queue run-token writes while the keeper is down, replay later.

``PG_OUTBOX`` names the run's outbox file (JSONL, created 0600; only run-token
sessions should set it).  A ``heartbeat`` / ``add_evidence`` / ``submit`` /
``verdict`` that finds the keeper unreachable is appended with a fresh 32-hex id;
``pg replay`` posts the queue (in order, ≤ 100 per call) with the same token and
keeps only the entries the keeper did not answer.  The keeper re-checks every entry
exactly like a live call and answers a repeated id ``duplicate`` (never re-run), so
a tampered or replayed file can only cause what the token holder could do anyway.
The token itself is never written to the outbox.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time

__all__ = ["QUEUE_VERBS", "EXIT_QUEUED", "on_unreachable", "cmd_replay", "add_parser"]

QUEUE_VERBS = frozenset({"heartbeat", "add_evidence", "submit", "verdict"})
EXIT_QUEUED = 4
_BATCH = 100


def _path():
    return os.environ.get("PG_OUTBOX") or ""


def on_unreachable(verb, body, exc, *, exit_unreachable=3) -> int:
    """Queue *verb* when an outbox is configured; else the plain unreachable exit."""
    path = _path()
    if not path or verb not in QUEUE_VERBS:
        print(f"keeper unreachable: {exc}", file=sys.stderr)
        return exit_unreachable
    entry = {"id": secrets.token_hex(16), "verb": verb, "body": body,
             "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    print(f"keeper unreachable: queued {verb} in the outbox ({entry['id']})", file=sys.stderr)
    return EXIT_QUEUED


def _read(path):
    entries = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue            # a damaged line is dropped, never sent
                if isinstance(entry, dict):
                    entries.append(entry)
    return entries


def cmd_replay(args, session) -> int:
    from .cli_verbs import EXIT_OK, EXIT_UNREACHABLE, RefusedCli, Unreachable
    path = _path()
    if not path or not os.path.exists(path):
        print("no outbox (PG_OUTBOX unset or empty)", file=sys.stderr)
        return EXIT_OK
    entries, answered, results = _read(path), set(), []
    try:
        for start in range(0, len(entries), _BATCH):
            chunk = entries[start:start + _BATCH]
            out = session.call("POST", "replay", body={"entries": chunk})
            for res in out.get("results") or []:
                answered.add(res.get("id"))
                results.append(res)
    except Unreachable as exc:
        print(f"keeper still unreachable: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except RefusedCli as exc:
        print(f"replay refused: {exc}", file=sys.stderr)
        return 2
    finally:
        left = [e for e in entries if e.get("id") not in answered]
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(e, sort_keys=True) + "\n" for e in left)
        os.replace(tmp, path)
    for res in results:
        print(f"{res.get('id')} {res.get('result')}")
    return EXIT_OK


def add_parser(sub, make):
    make(sub, "replay", help="M2B: deliver the offline outbox (PG_OUTBOX) with the run token")
