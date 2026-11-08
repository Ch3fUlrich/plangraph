"""Snapshot-only fleet projection; no probing, sampling or transport."""
from __future__ import annotations

import math
import os
from pathlib import Path

METRICS = (
    "cpu_percent", "ram_available_bytes", "gpu_percent",
    "network_rx_bytes_per_s", "network_tx_bytes_per_s", "disk_free_bytes",
)


def build_fleet(registry, samples=None, runs=None):
    """Project persisted inputs through the spawner's private Rust subcommand."""
    import json
    import shutil
    import subprocess

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: finite(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [finite(item) for item in value]
        return value

    binary = Path(__file__).resolve().parents[2] / 'rust/target/release/pg-spawnd'
    binary = str(binary) if binary.is_file() else (shutil.which('pg-spawnd') or 'pg-spawnd')
    try:
        result = subprocess.run([binary, 'fleet-snapshot'], check=False,
            input=json.dumps(finite(dict(registry=registry,
                samples=list((samples or {}).items()), runs=runs))),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if result.returncode:
            reason = result.stderr.strip()
            if reason not in ('ambiguous registry alias',
                    'registry aliases must be a list or tuple of strings',
                    'registry alias needs a nonempty string',
                    'registry host needs an id', 'duplicate registry host'):
                reason = 'fleet projection failed'
            raise ValueError(reason)
        data = json.loads(result.stdout)
        rows = [{key: row[key] for key in ('host_id', 'class', 'caps', 'sample',
                 'samples', 'running_agents')} for row in data['hosts']]
        for row in rows:
            row['sample'] = {key: row['sample'][key] for key in METRICS}
        return dict(hosts=rows, running_agents=data['running_agents'])
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError) as exc:
        raise RuntimeError('fleet projection failed') from None


def load_fleet(store, samples=None):
    """Read persisted registry and existing local run records, without side effects.

    Slice 2 supplies the samples API's host->metrics mapping here. The current
    bus registry has no class/caps columns, so those remain unknown as well.
    """
    from .keeper.run_records import read_run_records

    state = Path(os.environ.get("PLANGRAPH_SPAWN_STATE") or
                 Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
                 / "plangraph")
    runs = read_run_records(state)
    return build_fleet(store.list_bus_hosts(), samples=samples, runs=runs)
