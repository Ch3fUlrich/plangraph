"""Snapshot-only fleet projection; no probing, sampling or transport."""
from __future__ import annotations

import math
import os
import threading
from pathlib import Path

from .keeper.run_records import RunRecordCache

METRICS = (
    "cpu_percent", "ram_available_bytes", "gpu_percent",
    "network_rx_bytes_per_s", "network_tx_bytes_per_s", "disk_free_bytes",
)
PG_SPAWND_MISSING = 'pg-spawnd binary missing (build the release helper)'


def _projection_binary():
    """Resolve the pg-spawnd executable shared by projection and native launch."""
    import shutil

    binary = shutil.which('pg-spawnd')
    if binary is not None:
        return binary

    # Non-login service units may omit the host-sync install directory from PATH.
    installed = Path.home() / '.local/bin'
    release = Path(__file__).resolve().parents[2] / 'rust/target/release'
    candidates = [directory / name for directory in (installed, release)
                  for name in ('pg-spawnd', 'pg-spawnd.exe')]
    for binary in candidates:
        if binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    path = os.pathsep.join(os.get_exec_path())
    tried = ', '.join(map(str, candidates))
    raise RuntimeError(f'{PG_SPAWND_MISSING}: tried pg-spawnd on PATH ({path}), {tried}')


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


class FleetProjection:
    """Serialize fleet work and retain the projection while its inputs are unchanged."""

    def __init__(self):
        self.lock = threading.Lock()
        self.runs = RunRecordCache(fields=("run_id", "host", "status"))
        self.inputs = self.value = None

    def load(self, state, registry, samples):
        with self.lock:
            runs = self.runs.read(state)
            inputs = (registry, samples, runs)
            if inputs != self.inputs:
                self.value = build_fleet(registry, samples=samples, runs=runs)
                self.inputs = inputs
            return self.value


def load_fleet(store, samples=None, *, registry=None, projection=None):
    """Read persisted registry and local runs, projecting only fields pg-spawnd consumes."""
    state = Path(os.environ.get("PLANGRAPH_SPAWN_STATE") or
                 Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
                 / "plangraph")
    if registry is None:
        registry = store.list_bus_hosts()
    return (projection or FleetProjection()).load(state, registry, samples)
