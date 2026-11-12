"""Snapshot-only fleet projection; no probing, sampling or transport."""
from __future__ import annotations

import math
import os
from pathlib import Path

METRICS = (
    "cpu_percent", "ram_available_bytes", "gpu_percent",
    "network_rx_bytes_per_s", "network_tx_bytes_per_s", "disk_free_bytes",
)
PG_SPAWND_MISSING = 'pg-spawnd binary missing (build the release helper)'


def _projection_binary():
    import shutil

    release = Path(__file__).resolve().parents[2] / 'rust/target/release'
    for name in ('pg-spawnd', 'pg-spawnd.exe'):
        binary = release / name
        if binary.is_file():
            return str(binary)
    binary = shutil.which('pg-spawnd')
    if binary is None:
        raise RuntimeError(PG_SPAWND_MISSING)
    return binary


def build_fleet(registry, samples=None, runs=None):
    """Project persisted inputs through the spawner's private Rust subcommand."""
    import json
    import subprocess

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: finite(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [finite(item) for item in value]
        return value

    binary = _projection_binary()
    try:
        # Opaque class/caps metadata stays in Python, with its original types.
        registry = list(registry)
        projected = [{key: value for key, value in row.items()
                      if key in ('id', 'host_id', 'host', 'aliases')}
                     for row in registry]
        result = subprocess.run([binary, 'fleet-snapshot'], check=False,
            input=json.dumps(finite(dict(registry=projected,
                samples=list((samples or {}).items()), runs=runs))),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if result.returncode:
            raise RuntimeError('fleet projection failed')
        data = json.loads(result.stdout)
        error = data.get('error')
        if error is not None:
            # Fixed validation errors belong to the private protocol, not stderr.
            if not isinstance(error, str):
                raise TypeError()
            raise ValueError(error)
        rows = [{key: row[key] for key in ('host_id', 'class', 'caps', 'sample',
                 'samples', 'running_agents')} for row in data['hosts']]
        for row in rows:
            row['sample'] = {key: row['sample'][key] for key in METRICS}
        for row, projected in zip(rows, data['hosts']):
            original = registry[projected['registry_index']]
            row.update({key: original.get(key) for key in ('class', 'caps')})
        return dict(hosts=rows, running_agents=data['running_agents'])
    except FileNotFoundError:
        raise RuntimeError(PG_SPAWND_MISSING) from None
    except (OSError, subprocess.TimeoutExpired, KeyError, IndexError, TypeError,
            AttributeError, json.JSONDecodeError, UnicodeError):
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
