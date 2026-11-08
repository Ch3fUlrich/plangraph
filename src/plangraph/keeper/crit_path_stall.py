"""Read-only critical-dependency stall alerts, delivered through fleet-bus.

Like the keeper's ``alerts`` verb this is a read consumer, not a keeper
mutation. State lives beside keeper.json; flock serializes timer/manual runs.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .cli_verbs import Session, parse_duration

TARGET = "plangraph:RT-P1-E2E"


def open_dependencies(items, target):
    index = {item["key"]: item for item in items}
    if target not in index:
        raise ValueError(f"target absent from keeper snapshot: {target}")
    seen = {target}
    pending = list(index[target].get("depends_on") or [])
    result = []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        item = index[key]  # incomplete visibility must fail, not silently omit
        if item.get("state") in ("done", "cancelled"):
            continue
        result.append(item)
        pending.extend(item.get("depends_on") or [])
    return sorted(result, key=lambda item: item["key"])


def run(session, state_path, send, *, target=TARGET, thresholds=(43200, 86400),
        routing="routing", dry_run=False, now=None):
    """Return emitted lines; persist successful deliveries individually.

    A bus success includes durable spool acceptance. A process crash between
    send and save can replay a delivery (no cross-service transaction exists).
    """
    now = time.time() if now is None else now
    items = session.call("GET", "snapshot")["items"]
    candidates = open_dependencies(items, target)
    path = Path(state_path)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    # Dry-run does not even create a lock or state directory.
    if dry_run:
        return _scan(session, candidates, path, send, target, thresholds,
                     routing, True, now)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _scan(session, candidates, path, send, target, thresholds,
                     routing, False, now)


def _save(path, state):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, sort_keys=True) + "\n")
    os.replace(temp, path)


def _scan(session, candidates, path, send, target, thresholds, routing, dry, now):
    state = json.loads(path.read_text()) if path.exists() else {}
    lines = []
    for item in candidates:
        if item.get("state") not in ("claimed", "in_review"):
            continue
        key = item["key"]
        info = session.call("GET", "show", {"key": key})
        activity = info.get("last_activity")
        if not activity or not activity.get("at"):
            raise ValueError(f"missing keeper activity: {key}")
        at = activity["at"]
        parsed = datetime.fromisoformat(at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        stamp = parsed.timestamp()
        episode = [target, item["state"], at, activity.get("type"),
                   activity.get("actor"), item.get("epoch")]
        old = state.get(key, {})
        entry = old if old.get("episode") == episode else {
            "episode": episode, "sent_levels": []}
        if "sent_levels" not in entry:
            # Upgrade the original default-threshold delivery records. Preserve
            # their level rather than coupling future dedupe to duration values.
            entry["sent_levels"] = [
                [0 if seconds <= 43200 else 1, recipient]
                for seconds, recipient in entry.get("sent", [])]
        state[key] = entry
        owner = info.get("owner") or info.get("claimed_by")
        if not owner:
            raise ValueError(f"missing owner: {key}")
        recipients = list(dict.fromkeys((owner, routing)))
        for level, threshold in enumerate(thresholds):
            if now - stamp <= threshold:
                continue
            line = (f"critical-path-stall target={target} item={key} "
                    f"state={item['state']} idle>{threshold / 3600:g}h "
                    f"owner={owner} routing={routing}")
            pending = [r for r in recipients
                       if [level, r] not in entry["sent_levels"]]
            if not pending:
                continue
            print(line)
            lines.append(line)
            if dry:
                continue
            for recipient in pending:
                send(recipient, line)
                entry["sent_levels"].append([level, recipient])
                state[key] = entry
                _save(path, state)
    if not dry:
        _save(path, state)
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--url")
    parser.add_argument("--target", default=TARGET)
    parser.add_argument("--thresholds", nargs=2, type=parse_duration,
                        default=(43200, 86400), metavar=("FIRST", "SECOND"))
    parser.add_argument("--routing", default="routing")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.thresholds[0] < args.thresholds[1]:
        parser.error("thresholds must be positive and increasing")
    session = Session(config_path=args.config, url=args.url)
    state = Path(session.config.db).parent / "crit-path-stall.json"
    def send(recipient, line):
        from plangraph.fleet_bus.client import build_client
        return build_client().send(recipient, line)
    run(session, state, send, target=args.target, thresholds=args.thresholds,
        routing=args.routing, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
