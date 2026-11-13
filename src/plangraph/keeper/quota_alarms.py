"""Subscription-pool quota alarms: one routing line per 50/75/90 % threshold per window.

Reads quota_snapshots (never raw_data or key tables) read-only, either straight from
a sqlite file (``--db`` / QUOTA_SNAPSHOT_DB: a read-only mount or copy of the gateway
data, no docker access needed) or, when unset, from the gateway container via
``docker exec`` with the script on stdin. Each newly crossed threshold goes through
fleet-bus. State dedups per (provider, connection, window id, threshold): the window id
is next_reset_at, or for a null reset the start of the newest run (a refill of
ROLLOVER_RISE points starts a new run, remembered in state). A window without a
snapshot for STALE_HOURS is never silent: a stale/blind notice line is sent and printed
(repeated every STALE_REMIND_HOURS while it lasts).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

THRESHOLDS = (50, 75, 90)
BURN_HOURS = 6
STALE_HOURS = 3  # snapshots are hourly; allow two missed ones plus jitter
STALE_REMIND_HOURS = 24
ROLLOVER_RISE = 10  # remaining% rising this much within a null-reset series = refill
CONTAINER = os.environ.get("QUOTA_GATEWAY_CONTAINER", "autoos-omniroute")
SQL = ("SELECT provider,connection_id,window_key,remaining_percentage,is_exhausted,"
       "next_reset_at,created_at FROM quota_snapshots WHERE created_at>=? OR id IN "
       "(SELECT max(id) FROM quota_snapshots GROUP BY provider,connection_id,window_key) "
       "ORDER BY id")
JS = ('const D=require("better-sqlite3");'
      'const d=new D("/app/data/storage.sqlite",{readonly:true});'
      f'console.log(JSON.stringify(d.prepare({json.dumps(SQL)}).all(process.argv[2]||"")));')


def fetch_rows(since_iso, container=CONTAINER):
    out = subprocess.run(
        ["docker", "exec", "-i", "-w", "/app", container, "node", "-", since_iso],
        input=JS, capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


def fetch_rows_db(since_iso, db_path):
    """Read the snapshots from a sqlite file opened read-only: no docker, no write."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        cursor = con.execute(SQL, (since_iso,))
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]
    finally:
        con.close()


def _ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _gid(provider, conn, window):
    return "|".join([provider, conn or "", window])


def _groups(rows):
    """Series per (provider, connection, window), each sorted oldest to newest."""
    groups = {}
    for r in rows:
        groups.setdefault((r["provider"], r["connection_id"], r["window_key"]), []).append(r)
    for series in groups.values():
        series.sort(key=lambda r: _ts(r["created_at"]))  # stable: ties keep id order
    return groups


def _newest_run(series):
    """Rows of the newest window: same reset time; for a null reset also cut at a refill."""
    last = series[-1]
    run = [r for r in series if r["next_reset_at"] == last["next_reset_at"]]
    if last["next_reset_at"] is None:
        for i in range(len(run) - 1, 0, -1):
            a, b = run[i - 1]["remaining_percentage"], run[i]["remaining_percentage"]
            if a is not None and b is not None and b - a >= ROLLOVER_RISE:
                return run[i:], True
    return run, False


def _live(last, now):
    """A window is live while it has a known level and its reset time has not passed."""
    return last["remaining_percentage"] is not None and not (
        last["next_reset_at"] and _ts(last["next_reset_at"]) <= now)


def evaluate(rows, now):
    """Return one dict per fresh live window with its newest used%, burn rate and projection."""
    result = []
    for (provider, conn, window), series in _groups(rows).items():
        last = series[-1]
        if not _live(last, now) or _ts(last["created_at"]) < now - STALE_HOURS * 3600:
            continue  # unknown level, rolled over, or stale (see reader_notice)
        used = 100 - last["remaining_percentage"]
        run, rolled = _newest_run(series)
        cur = [r for r in run if r["remaining_percentage"] is not None
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
                       "empty": empty, "run_start": run[0]["created_at"], "rolled": rolled})
    return result


def reader_notice(rows, now):
    """(key, line) when a live window has no snapshot for STALE_HOURS or no snapshot is fresh.

    "blind" means nothing at all is fresh (writer stopped, table empty); "stale" means only
    some live windows lost their snapshots. Stale windows are skipped by evaluate(), so this
    line is what keeps them from going silent.
    """
    stale = []
    for (provider, conn, window), series in _groups(rows).items():
        last = series[-1]
        if _live(last, now) and _ts(last["created_at"]) < now - STALE_HOURS * 3600:
            stale.append((_gid(provider, conn, window), f"{provider}/{window}", last["created_at"]))
    newest = max((r["created_at"] for r in rows), key=_ts, default=None)
    blind = newest is None or _ts(newest) < now - STALE_HOURS * 3600
    if not stale and not blind:
        return None
    stale.sort()
    detail = ", ".join(f"{name} last={at}" for _, name, at in stale[:6]) or "no live window"
    line = (f"quota-alarm-{'blind' if blind else 'stale'} {len(stale)} window(s) without a "
            f"snapshot in {STALE_HOURS}h: {detail}; newest snapshot={newest}; "
            "their thresholds are not evaluated")
    ident = "|".join(f"{g}@{at}" for g, _, at in stale) or f"blind@{newest}"
    return "stale|" + hashlib.sha1(ident.encode()).hexdigest()[:12], line


def format_line(w):
    rate = "n/a" if w["rate"] is None else f"{w['rate']:.1f}%/h"
    empty = "n/a" if w["empty"] is None else w["empty"].strftime("%Y-%m-%dT%H:%MZ")
    return (f"quota-alarm {w['provider']}/{w['window']} conn={(w['connection'] or '')[:8]} "
            f"used={w['used']:g}% >={w['threshold']}% burn6h={rate} "
            f"projected_empty={empty} resets={w['reset']}")


def _window_id(w, epochs):
    """next_reset_at, or for a null reset the remembered start of the newest run."""
    if w["reset"]:
        return w["reset"]
    gid = _gid(w["provider"], w["connection"], w["window"])
    known = epochs.get(gid)
    if known is None or (w["rolled"] and _ts(w["run_start"]) > _ts(known)):
        epochs[gid] = w["run_start"]  # first sight, or a refill newer than the known run
    return "since:" + epochs[gid]


def _prune(state, now):
    """Drop dedup keys of windows that cannot recur: reset passed, run superseded, notice aged."""
    for key, v in list(state["sent"].items()):
        wid = v.get("wid")
        if wid is None:
            live = _ts(v["at"]) > now - STALE_REMIND_HOURS * 3600
        elif wid.startswith("since:"):
            live = "since:" + state["epochs"].get(v["gid"], "") == wid
        else:
            live = _ts(wid) > now
        if not live:
            del state["sent"][key]


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
    os.replace(tmp, path)


def run(rows, state_path, send, *, now=None, dry_run=False):
    """Emit one line per newly crossed threshold (and stale notice); save state per delivery."""
    now = time.time() if now is None else now
    path = Path(state_path)
    state = json.loads(path.read_text()) if path.exists() else {}
    state.setdefault("sent", {})
    state.setdefault("epochs", {})
    before = json.dumps(state, sort_keys=True)
    _prune(state, now)
    lines = []

    def emit(key, line, meta):
        lines.append(line)
        if not dry_run:
            send(line)
            state["sent"][key] = {"at": datetime.fromtimestamp(now, UTC).isoformat(), **meta}
            _save(path, state)

    windows = evaluate(rows, now)
    for w in windows:
        wid = _window_id(w, state["epochs"])
        for th in THRESHOLDS:
            key = "|".join([_gid(w["provider"], w["connection"], w["window"]), wid, str(th)])
            if w["used"] >= th and key not in state["sent"]:
                emit(key, format_line({**w, "threshold": th}),
                     {"wid": wid, "gid": _gid(w["provider"], w["connection"], w["window"])})
    notice = reader_notice(rows, now)
    if notice and notice[0] not in state["sent"]:
        emit(notice[0], notice[1], {})
    if not dry_run and json.dumps(state, sort_keys=True) != before:
        _save(path, state)
    return lines


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default=str(Path.home() / ".local/state/plangraph/quota-alarms.json"))
    p.add_argument("--routing", default="routing")
    p.add_argument("--db", default=os.environ.get("QUOTA_SNAPSHOT_DB"),
                   help="sqlite file to read read-only instead of docker exec")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    now = time.time()
    since = (datetime.fromtimestamp(now, UTC) - timedelta(hours=BURN_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = fetch_rows_db(since, a.db) if a.db else fetch_rows(since)

    def send(line):
        from plangraph.fleet_bus.client import build_client
        return build_client().send(a.routing, line)

    lock = Path(a.state).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # same lock for dry-run and real runs
        lines = run(rows, a.state, send, now=now, dry_run=a.dry_run)
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
