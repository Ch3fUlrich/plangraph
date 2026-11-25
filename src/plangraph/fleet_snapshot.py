"""Snapshot-only fleet projection; no probing, sampling or transport.

It is also the one place that locates ``pg-spawnd``: the native launch and the duplicate-assignment
guard (``lane_check``, the Python entry to ``pg-spawnd lane-check``) reach the binary here.
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
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


#: What the Rust guard prints when it refuses a submission.
_REFUSALS = ("lane_conflict:", "lane_override requires")


class LaneRefused(ValueError):
    """An item is held by another live lane, an override came without a reason, or (strict)
    the guard could not give a decision."""


def lane_state() -> Path:
    return Path(os.environ.get("PLANGRAPH_SPAWN_STATE") or
                Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "plangraph")


def keeper_lanes(keys):
    """``(lanes, complete)``: the keeper's open lanes of ``keys`` (``work_exists.lanes`` of
    ``show``; a branch whose pull request closed is not among them), and whether every lookup
    answered. A failed lookup keeps the lanes the other keys returned and makes the result
    incomplete; no configured keeper (``PG_URL`` unset) has nothing to ask and is complete."""
    keys = [key for key in dict.fromkeys(keys) if isinstance(key, str) and key]
    if not keys or not os.environ.get("PG_URL"):
        return [], True
    lanes, complete = [], True
    try:
        from plangraph.keeper.cli_verbs import Session
        session = Session()
    except Exception as exc:
        logging.warning("keeper open lanes unavailable: %s", exc)
        return [], False
    for key in keys:
        try:
            found = (session.call("GET", "show", {"key": key}).get("work_exists") or {}).get("lanes")
            if not isinstance(found, list):
                raise ValueError("show returned no lane list")
            lanes += [lane for lane in found if isinstance(lane, str)]
        except Exception as exc:  # one failed lookup must not discard the others' lanes
            logging.warning("keeper open lanes of %s unavailable: %s", key, exc)
            complete = False
    return list(dict.fromkeys(lanes)), complete


def lane_check(payload: dict, binary: str | None = None, *, strict: bool = False) -> None:
    """Refuse (``LaneRefused``) a submission another live lane holds.

    ``payload`` is the ``lane-check`` input (``state``, ``prompt``, ``card``, ``keys``,
    ``reason``, ``reserve`` ...); the keeper's open lanes of its items are added. ``strict`` is
    the dispatch boundary: no decision (no binary, a timeout, an unwritable override log, any
    failure) refuses, so a run is published only after an admission, a reservation and an
    override write succeeded. Otherwise a host that cannot answer is skipped with a warning.
    """
    def unanswered(message):
        if strict:
            raise LaneRefused(f"lane guard gave no decision: {message}")
        logging.warning("lane guard skipped: %s", message)

    if binary is None:
        try:
            binary = _projection_binary()
        except RuntimeError:
            return unanswered("pg-spawnd is not installed")
    payload = {"state": str(lane_state()), **payload}
    if "branches" not in payload:
        # Ask the keeper about every canonical key the guard will check (prompt, card alias,
        # PLANGRAPH_SPAWN_ITEM, listed items), as the guard itself resolves them.
        try:
            keys = json.loads(subprocess.run(
                [binary, "lane-keys"], input=json.dumps(payload), text=True, capture_output=True,
                timeout=30, check=True).stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return unanswered(f"lane-keys: {exc}")
        payload["branches"], complete = keeper_lanes(keys)
        if strict and not complete:
            # Never reserve or record on an incomplete picture: a preview names a conflict the
            # known lanes already show, else the lookup failure itself refuses.
            lane_check({**payload, "record": False, "reserve": None}, binary)
            raise LaneRefused("lane guard gave no decision: the keeper's open lanes are incomplete")
    try:
        result = subprocess.run([binary, "lane-check"], input=json.dumps(payload), text=True,
                                capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return unanswered(str(exc))
    if result.returncode == 0:
        return
    message = (result.stderr or result.stdout).strip()
    if message.startswith(_REFUSALS):
        raise LaneRefused(message)
    unanswered(message[:200])
