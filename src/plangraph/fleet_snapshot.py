"""Snapshot-only fleet projection; no probing, sampling or transport."""
from __future__ import annotations

import math
import os
from collections import Counter
from pathlib import Path

METRICS = (
    "cpu_percent", "ram_available_bytes", "gpu_percent",
    "network_rx_bytes_per_s", "network_tx_bytes_per_s", "disk_free_bytes",
)


def _metric(value, key):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0
            or key in ("cpu_percent", "gpu_percent") and value > 100):
        return None
    return value


def build_fleet(registry, samples=None, runs=None):
    """Project registry rows, injected host->metrics, and authoritative run rows.

    Missing run records are unknown, unlike an explicitly empty list. Duplicate
    host/run pairs count once; conflicting or malformed records fail unknown.
    Registry aliases resolve samples and runs without exposing private fields.
    """
    samples = {} if samples is None else samples
    hosts, aliases = {}, {}
    for row in registry:
        host = row.get("id") or row.get("host_id") or row.get("host")
        if not isinstance(host, str) or not host:
            raise ValueError("registry host needs an id")
        if host in hosts:
            raise ValueError(f"duplicate registry host: {host}")
        hosts[host] = row
        row_aliases = row.get("aliases", [])
        if not isinstance(row_aliases, (list, tuple)):
            raise ValueError("registry aliases must be a list or tuple of strings")
        for alias in [host, *row_aliases]:
            if not isinstance(alias, str) or not alias:
                raise ValueError("registry alias needs a nonempty string")
            if alias in aliases and aliases[alias] != host:
                raise ValueError(f"ambiguous registry alias: {alias}")
            aliases[alias] = host
    counts, seen = Counter(), {}
    known = runs is not None
    for run in runs or []:
        host = aliases.get(run.get("host"))
        run_id, status = run.get("run_id"), run.get("status")
        if (host is None or not isinstance(run_id, str) or not run_id
                or status not in ("running", "completed", "failed", "cancelled",
                                  "queued", "dry_run", "refused")):
            known = False
            continue
        pair = (host, run_id)
        if pair in seen and seen[pair] != status:
            known = False
        seen[pair] = status
    for (host, _), status in seen.items():
        if status == "running":
            counts[host] += 1
    canonical_samples = {aliases[key]: value for key, value in samples.items()
                         if key in aliases}
    rows = []
    for host, row in sorted(hosts.items()):
        sample = canonical_samples.get(host)
        available = isinstance(sample, dict)
        sample = sample if available else {}
        rows.append({
            "host_id": host, "class": row.get("class"), "caps": row.get("caps"),
            "sample": {key: _metric(sample.get(key), key) for key in METRICS},
            "samples": "available" if available else "unavailable",
            "running_agents": counts[host] if known else None,
        })
    return {"hosts": rows, "running_agents": sum(counts.values()) if known else None}


def load_fleet(store, samples=None):
    """Read persisted registry and existing local run records, without side effects.

    Slice 2 supplies the samples API's host->metrics mapping here. The current
    bus registry has no class/caps columns, so those remain unknown as well.
    """
    from .planner.routing import read_run_records

    state = Path(os.environ.get("PLANGRAPH_SPAWN_STATE") or
                 Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
                 / "plangraph")
    runs = read_run_records(state)
    return build_fleet(store.list_bus_hosts(), samples=samples, runs=runs)
