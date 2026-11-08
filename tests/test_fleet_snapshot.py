"""Hermetic fleet projection and persisted snapshot failure isolation."""
import json
from types import SimpleNamespace

import pytest

from plangraph import fleet_snapshot as fleet
from plangraph.keeper.snapshot_io import write_snapshot

REGISTRY = [{"id": "a", "aliases": ["alias"], "class": "worker",
             "caps": {"max_runs": 4}}, {"id": "b"}]


def test_fleet_samples_and_missing_host():
    sample = dict(zip(fleet.METRICS, [12.5, 1024, 0, 20, 30, 4096]))
    result = fleet.build_fleet(REGISTRY, {"alias": sample, "stranger": sample}, [])
    a, b = result["hosts"]
    assert a["sample"] == sample
    assert a["class"] == "worker" and a["caps"] == {"max_runs": 4}
    assert b["sample"] == dict.fromkeys(fleet.METRICS)
    assert b["samples"] == "unavailable"
    assert result["running_agents"] == 0
    assert [r["host_id"] for r in result["hosts"]] == ["a", "b"]


@pytest.mark.parametrize("value", [None, "unreadable", True, -1, float("nan"), float("inf")])
def test_fleet_unreadable_is_null(value):
    sample = dict.fromkeys(fleet.METRICS, value)
    result = fleet.build_fleet(REGISTRY, {"a": sample}, [])
    assert result["hosts"][0]["sample"] == dict.fromkeys(fleet.METRICS)


def test_fleet_percent_range_and_measured_zero():
    result = fleet.build_fleet(REGISTRY, {"a": {"cpu_percent": 101, "gpu_percent": 0}}, [])
    assert result["hosts"][0]["sample"]["cpu_percent"] is None
    assert result["hosts"][0]["sample"]["gpu_percent"] == 0


def test_fleet_counts_live_runs_only():
    runs = [{"host": host, "run_id": ident, "status": status} for host, ident, status in
            [("alias", "one", "running"), ("a", "one", "running"),
             ("b", "two", "running"), ("a", "three", "completed"),
             ("a", "four", "queued"), ("a", "five", "failed")]]
    result = fleet.build_fleet(REGISTRY, runs=runs)
    assert result["running_agents"] == 2
    assert [r["running_agents"] for r in result["hosts"]] == [1, 1]


@pytest.mark.parametrize("runs", [None, [{}],
    [{"host": "unknown", "run_id": "x", "status": "running"}],
    [{"host": "a", "run_id": "x", "status": status} for status in ("running", "completed")]])
def test_fleet_unknown_counts(runs):
    result = fleet.build_fleet(REGISTRY, runs=runs)
    assert result["running_agents"] is None
    assert all(r["running_agents"] is None for r in result["hosts"])


def test_fleet_loader_existing_records(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    store = SimpleNamespace(list_bus_hosts=lambda: [{"host": "a"}, {"host": "b"}])
    assert fleet.load_fleet(store)["running_agents"] is None
    record = tmp_path / "runs/run-one/record.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"host": "a", "run_id": "one", "status": "running"}))
    result = fleet.load_fleet(store)
    assert result["running_agents"] == 1
    assert all(r["samples"] == "unavailable" for r in result["hosts"])
    record.write_text("broken json")
    assert fleet.load_fleet(store)["running_agents"] is None
    assert not (tmp_path / "watchdog").exists()


@pytest.mark.parametrize("fails", [False, True])
def test_fleet_loader_exception_snapshot_still_written(tmp_path, monkeypatch, fails):
    def load(store):
        if fails:
            raise OSError("unreadable")
        return fleet.build_fleet(REGISTRY, runs=[])

    monkeypatch.setattr(fleet, "load_fleet", load)
    path = tmp_path / "snapshot.json"
    server = SimpleNamespace(config=SimpleNamespace(snapshot=path),
                             store=SimpleNamespace(snapshot=lambda who: {"items": []}))
    write_snapshot(server, object())
    body = json.loads(path.read_text())
    assert body["items"] == []
    assert body["fleet"] == (None if fails else fleet.build_fleet(REGISTRY, runs=[]))
