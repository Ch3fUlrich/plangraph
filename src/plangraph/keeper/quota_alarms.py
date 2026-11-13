"""Subscription-pool quota alarms: one routing line per 50/75/90 % threshold per window.

Reads quota_snapshots (never raw_data or key tables) read-only from the gateway
container's sqlite via ``docker exec`` with the script on stdin, then sends each
newly crossed threshold through fleet-bus. State dedups per
(provider, connection, window, next_reset_at, threshold); a new reset time is a new window.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

THRESHOLDS = (50, 75, 90)
BURN_HOURS = 6
STALE_HOURS = 1
CONTAINER = os.environ.get("QUOTA_GATEWAY_CONTAINER", "autoos-omniroute")
JS = """
const D=require("better-sqlite3");
const d=new D("/app/data/storage.sqlite",{readonly:true});
const cols="provider,connection_id,window_key,remaining_percentage,is_exhausted,next_reset_at,created_at";
console.log(JSON.stringify(d.prepare("SELECT "+cols+" FROM quota_snapshots WHERE created_at>=? OR id IN (SELECT max(id) FROM quota_snapshots GROUP BY provider,connection_id,window_key) ORDER BY id").all(process.argv[2]||"")));
"""


def fetch_rows(since_iso, container=CONTAINER):
    out = subprocess.run(
        ["docker", "exec", "-i", "-w", "/app", container, "node", "-", since_iso],
        input=JS, capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


def _ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def evaluate(rows, now):
    """Return one dict per window with its newest used%, burn rate and projection."""
    groups = {}
    for r in rows:  # rows are ordered oldest to newest
        groups.setdefault((r["provider"], r["connection_id"], r["window_key"]), []).append(r)
    result = []
    for (provider, conn, window), series in groups.items():
        last = series[-1]
        if last["remaining_percentage"] is None:
            continue
        if _ts(last["created_at"]) < now - STALE_HOURS * 3600:
            continue  # no fresh snapshot: the window is not live
        if last["next_reset_at"] and _ts(last["next_reset_at"]) <= now:
            continue  # window already rolled over
        used = 100 - last["remaining_percentage"]
        cur = [r for r in series if r["next_reset_at"] == last["next_reset_at"]
               and r["remaining_percentage"] is not None
               and _ts(r["created_at"]) >= now - BURN_HOURS * 3600]
        rate = None
        if len(cur) >= 2:
            span = (_ts(cur[-1]["created_at"]) - _ts(cur[0]["created_at"])) / 3600
            if span > 0:
                rate = (cur[0]["remaining_percentage"] - cur[-1]["remaining_percentage"]) / span
        empty = None
        if rate and rate > 0:
            empty = datetime.fromtimestamp(
                _ts(last["created_at"]) + last["remaining_percentage"] / rate * 3600, UTC)
        result.append({"provider": provider, "connection": conn, "window": window,
                       "reset": last["next_reset_at"], "used": used, "rate": rate,
                       "empty": empty})
    return result


def format_line(w):
    rate = "n/a" if w["rate"] is None else f"{w['rate']:.1f}%/h"
    empty = "n/a" if w["empty"] is None else w["empty"].strftime("%Y-%m-%dT%H:%MZ")
    return (f"quota-alarm {w['provider']}/{w['window']} conn={(w['connection'] or '')[:8]} "
            f"used={w['used']:g}% >={w['threshold']}% burn6h={rate} "
            f"projected_empty={empty} resets={w['reset']}")


def run(rows, state_path, send, *, now=None, dry_run=False):
    """Emit one line per newly crossed threshold; save state after each delivery."""
    now = time.time() if now is None else now
    path = Path(state_path)
    state = json.loads(path.read_text()) if path.exists() else {}
    lines = []
    for w in evaluate(rows, now):
        for th in THRESHOLDS:
            if w["used"] < th:
                continue
            key = "|".join([w["provider"], w["connection"] or "", w["window"], w["reset"] or "", str(th)])
            if key in state:
                continue
            line = format_line({**w, "threshold": th})
            lines.append(line)
            if not dry_run:
                send(line)
                state[key] = datetime.fromtimestamp(now, UTC).isoformat()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
                os.replace(tmp, path)
    return lines


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default=str(Path.home() / ".local/state/plangraph/quota-alarms.json"))
    p.add_argument("--routing", default="routing")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    now = time.time()
    since = (datetime.fromtimestamp(now, UTC) - timedelta(hours=BURN_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = fetch_rows(since)

    def send(line):
        from plangraph.fleet_bus.client import build_client
        return build_client().send(a.routing, line)

    lock = Path(a.state).with_suffix(".lock")
    if a.dry_run:
        lines = run(rows, a.state, send, now=now, dry_run=True)
    else:
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            lines = run(rows, a.state, send, now=now)
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
