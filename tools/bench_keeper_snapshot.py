"""Bench: per-write latency and GET /v1/snapshot latency vs item count (KEEPER-SNAPSHOT-PG).

In-process keeper on 127.0.0.1:0, real HTTP, real snapshot writer.  Per N in --sizes: seed N
items (+ ~4 events/item of churn), then time --writes ``set_priority`` POSTs (each is a committed
write followed by the snapshot rewrite) and --reads ``GET /v1/snapshot``.  Prints a markdown table.

    PYTHONPATH=src PG_DSN=postgresql://... python3 tools/bench_keeper_snapshot.py --backend postgres
    PYTHONPATH=src python3 tools/bench_keeper_snapshot.py --backend sqlite --sizes 100,400,1000,2000
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from plangraph.keeper import api, auth, config, db as kdb
from plangraph.keeper.store import Store


def _items(n, offset=0):
    return [{"project": "bench", "id": f"L{offset + i}", "title": f"Lane {offset + i}",
             "kind": "lane", "track": "T1", "owner": "Bench", "state": "open",
             "estimate_h": 2, "estimate_src": "default", "depends_on": [],
             "done_criteria": "done", "evidence_kinds": ["any"], "operator": False,
             "description": "x" * 200, "source": "bench"} for i in range(n)]


def _call(base, token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def run(backend, n, writes, reads, extra=None):
    tmp = Path(tempfile.mkdtemp(prefix="bench-snap-"))
    cfg_doc = {"db": str(tmp / "keeper.db"), "snapshot": str(tmp / "plan.snapshot.json"),
               "bind": "127.0.0.1:0", "reap_interval": 3600, **(extra or {})}
    opts = {}
    if backend == "postgres":
        import os
        opts = {"backend": "postgres", "dsn": os.environ["PG_DSN"],
                "schema": "b" + uuid.uuid4().hex[:10]}
        cfg_doc.update(backend="postgres", dsn=opts["dsn"], pg_schema=opts["schema"])
    cfg_path = tmp / "keeper.json"
    cfg_path.write_text(json.dumps(cfg_doc))
    cfg_path.chmod(0o600)
    cfg = config.load_config(cfg_path)
    conn = kdb.open_db(cfg.db, create=True, **opts)
    store = Store(conn)
    token = auth.add_principal(conn, "router", "router", [])
    who = auth.authenticate(conn, token)
    for off in range(0, n, 200):
        lanes = _items(min(200, n - off), off)
        goal = {"project": "bench", "id": f"G{off}", "kind": "goal", "title": f"Goal {off}",
                "description": "g", "depends_on": [i["id"] for i in lanes]}
        store.create_items(who, lanes + [goal])
    for r in range(4):                                  # event churn (events >> items, like live)
        for i in range(0, n, max(1, n // 100)):
            store.set_priority(who, f"bench:L{i}", r)
    server = api.make_server(store, cfg, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    lat = []
    for i in range(writes):
        t = time.perf_counter()
        _call(base, token, "POST", "/v1/set_priority", {"key": f"bench:L{i % n}", "n": i % 5})
        lat.append((time.perf_counter() - t) * 1000)
    flush = getattr(server, "flush_snapshot", None)
    if flush:
        flush()
    rd = []
    for _ in range(reads):
        t = time.perf_counter()
        _call(base, token, "GET", "/v1/snapshot")
        rd.append((time.perf_counter() - t) * 1000)
    server.shutdown()
    server.server_close()
    conn.close()
    return {"n": n, "w_p50": statistics.median(lat), "w_p95": _pct(lat, 0.95), "w_max": max(lat),
            "r_p50": statistics.median(rd), "bytes": (tmp / "plan.snapshot.json").stat().st_size}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    ap.add_argument("--sizes", default="100,400,1000,2000")
    ap.add_argument("--writes", type=int, default=60)
    ap.add_argument("--reads", type=int, default=5)
    args = ap.parse_args(argv)
    print(f"| backend | items | write p50 ms | write p95 ms | write max ms | GET snapshot p50 ms | snapshot bytes |")
    print("|---|---|---|---|---|---|---|")
    for n in (int(s) for s in args.sizes.split(",")):
        r = run(args.backend, n, args.writes, args.reads)
        print(f"| {args.backend} | {r['n']} | {r['w_p50']:.0f} | {r['w_p95']:.0f} | {r['w_max']:.0f} | "
              f"{r['r_p50']:.0f} | {r['bytes']} |", flush=True)


if __name__ == "__main__":
    main()
