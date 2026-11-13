"""Hermetic fleet projection and persisted snapshot failure isolation."""
import json
from types import SimpleNamespace

import pytest

from _spawnd import require_spawnd
from plangraph import fleet_snapshot as fleet
from plangraph.keeper.snapshot_io import write_snapshot
from keeper_util import World
from plangraph.spawner.service import SpawnService

REGISTRY = [{"id": "a", "aliases": ["alias"], "class": "worker",
             "caps": {"max_runs": 4}}, {"id": "b"}]


@pytest.fixture
def spawnd():
    return require_spawnd()


def test_fleet_samples_and_missing_host(spawnd):
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
def test_fleet_unreadable_is_null(value, spawnd):
    sample = dict.fromkeys(fleet.METRICS, value)
    result = fleet.build_fleet(REGISTRY, {"a": sample}, [])
    assert result["hosts"][0]["sample"] == dict.fromkeys(fleet.METRICS)


def test_fleet_percent_range_and_measured_zero(spawnd):
    result = fleet.build_fleet(REGISTRY, {"a": {"cpu_percent": 101, "gpu_percent": 0}}, [])
    assert result["hosts"][0]["sample"]["cpu_percent"] is None
    assert result["hosts"][0]["sample"]["gpu_percent"] == 0


def test_fleet_counts_live_runs_only(spawnd):
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
def test_fleet_unknown_counts(runs, spawnd):
    result = fleet.build_fleet(REGISTRY, runs=runs)
    assert result["running_agents"] is None
    assert all(r["running_agents"] is None for r in result["hosts"])


def test_fleet_loader_existing_records(tmp_path, monkeypatch, spawnd):
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    world = World(tmp_path, backend="sqlite")
    try:
        world.store.upsert_bus_hosts([
            dict(host=h, node_id=h, key_id=h, endpoint=None,
                 health="offline", last_seen=None) for h in ("a", "b")])
        assert fleet.load_fleet(world.store)["running_agents"] is None
        service = SpawnService(state=tmp_path)
        for i, status in enumerate(("running", "completed", "failed", "cancelled",
                                    "queued", "dry_run", "refused")):
            monkeypatch.setenv("PLANGRAPH_HOST", "a")
            service._save(dict(run_id=f"run-{i}", status=status))
        result = fleet.load_fleet(world.store)
        assert result["running_agents"] == 1
        assert [r["running_agents"] for r in result["hosts"]] == [1, 0]
        assert all(r["sample"] == dict.fromkeys(fleet.METRICS) for r in result["hosts"])
        (tmp_path / "runs/run-0/record.json").write_text("broken json")
        assert fleet.load_fleet(world.store)["running_agents"] is None
        assert not (tmp_path / "watchdog").exists()
    finally:
        world.close()


def test_fleet_rust_failure_is_public(tmp_path, monkeypatch, spawnd):
    import subprocess
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(args, 2, '', f'{tmp_path}/private secret')
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='^fleet projection failed$'):
        fleet.load_fleet(SimpleNamespace(list_bus_hosts=lambda: []))


@pytest.mark.parametrize("fails", [False, True])
def test_fleet_loader_exception_snapshot_still_written(tmp_path, monkeypatch, fails, caplog, spawnd):
    def load(store):
        if fails:
            raise OSError(f"unreadable store: {tmp_path}/private/store.sqlite\nsecret detail")
        return fleet.build_fleet(REGISTRY, runs=[])

    monkeypatch.setattr(fleet, "load_fleet", load)
    path = tmp_path / "snapshot.json"
    server = SimpleNamespace(config=SimpleNamespace(snapshot=path),
                             store=SimpleNamespace(snapshot=lambda who: {"items": []}))
    write_snapshot(server, object())
    body = json.loads(path.read_text())
    assert body["items"] == []
    assert body["fleet"] == (None if fails else fleet.build_fleet(REGISTRY, runs=[]))
    if fails:
        assert body["fleet_error"] == "projection_failed"
        assert "Fleet snapshot projection failed" in caplog.text
        assert "OSError: projection unavailable" in caplog.text
        assert str(tmp_path) not in caplog.text
        assert "secret detail" not in caplog.text
        assert "Traceback" not in caplog.text
        assert len(caplog.records) == 1
        assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("registry", [
    [{"id": "a", "aliases": ["shared"]}, {"id": "b", "aliases": ["shared"]}],
    [{"id": "a", "aliases": ["b"]}, {"id": "b"}],
    [{"id": "a"}, {"id": "b", "aliases": ["a"]}],
])
def test_colliding_aliases_refused(registry, spawnd):
    with pytest.raises(ValueError, match="ambiguous registry alias"):
        fleet.build_fleet(registry, {"a": {"cpu_percent": 1}}, [])


@pytest.mark.parametrize("aliases", ["abc", None, {}, 1, [1], (None,)])
def test_invalid_aliases_refused(aliases, spawnd):
    with pytest.raises(ValueError, match="registry alias"):
        fleet.build_fleet([{"id": "a", "aliases": aliases}])


@pytest.mark.parametrize("aliases", [["alias"], ("alias",)])
def test_alias_containers_supported(aliases, spawnd):
    result = fleet.build_fleet([{"id": "a", "aliases": aliases}],
                              {"alias": {"cpu_percent": 12.5}}, [])
    assert result["hosts"][0]["sample"]["cpu_percent"] == 12.5


@pytest.mark.parametrize('ident', [0.0, False, '', None, float('nan'), float('inf')])
def test_opaque_metadata_and_zero_id(ident, spawnd):
    caps = {"tuple": (1, 2), "nan": float("nan"), "opaque": object()}
    cls = object()
    row = fleet.build_fleet([{"id": ident, "host_id": "a", "class": cls,
                              "caps": caps}])["hosts"][0]
    assert row["host_id"] == "a"
    assert row["caps"] is caps and row["class"] is cls


@pytest.mark.parametrize("failure", ["missing", "timeout", "exit", "keys", "json"])
def test_bridge_failure_snapshot_fallback(tmp_path, monkeypatch, caplog, failure, spawnd):
    import subprocess
    def fail(*args, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("private")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private", 10)
        return subprocess.CompletedProcess(args, int(failure == "exit"),
            "{}" if failure == "keys" else "usage: private", "private diagnostic")
    monkeypatch.setattr(subprocess, "run", fail)
    reason = (fleet.PG_SPAWND_MISSING if failure == 'missing'
              else 'fleet projection failed')
    with pytest.raises(RuntimeError) as error:
        fleet.build_fleet([])
    assert str(error.value) == reason
    monkeypatch.setattr(fleet, "load_fleet", lambda store: fleet.build_fleet([]))
    path = tmp_path / "snapshot.json"
    server = SimpleNamespace(config=SimpleNamespace(snapshot=path),
                             store=SimpleNamespace(snapshot=lambda who: {"items": []}))
    write_snapshot(server, object())
    body = json.loads(path.read_text())
    assert body["fleet"] is None and body["fleet_error"] == "projection_failed"
    assert "private" not in caplog.text


@pytest.mark.parametrize('ci', [None, 'true'])
def test_missing_binary_policy(tmp_path, monkeypatch, ci):
    import shutil
    import subprocess
    monkeypatch.setattr(fleet, '__file__', str(tmp_path / 'src/plangraph/fleet_snapshot.py'))
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    if ci is None:
        monkeypatch.delenv('CI', raising=False)
    else:
        monkeypatch.setenv('CI', ci)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('must not exec'))
    reason = 'pg-spawnd binary missing (build the release helper)'
    with pytest.raises(RuntimeError) as error:
        fleet.build_fleet([])
    assert str(error.value) == reason
    outcome = pytest.fail.Exception if ci else pytest.skip.Exception
    with pytest.raises(outcome) as error:
        require_spawnd()
    assert str(error.value) == reason


@pytest.mark.parametrize('name', ['pg-spawnd', 'pg-spawnd.exe', 'PATH'])
def test_projection_binary_lookup(tmp_path, monkeypatch, name):
    import shutil
    import subprocess
    monkeypatch.setattr(fleet, '__file__', str(tmp_path / 'src/plangraph/fleet_snapshot.py'))
    binary = tmp_path / 'rust/target/release' / name
    binary.parent.mkdir(parents=True)
    if name != 'PATH':
        binary.touch()
    monkeypatch.setattr(shutil, 'which', lambda command: str(tmp_path / 'PATH'))
    def run(command, **kwargs):
        assert command == [str(binary if name != 'PATH' else tmp_path / 'PATH'), 'fleet-snapshot']
        return subprocess.CompletedProcess(command, 0, '{"hosts": [], "running_agents": 0}')
    monkeypatch.setattr(subprocess, 'run', run)
    assert fleet.build_fleet([]) == dict(hosts=[], running_agents=0)


@pytest.mark.parametrize('registry,reason', [
    ([{}], 'registry host needs an id'),
    ([{'id': True, 'host_id': 'a'}], 'registry host needs an id'),
    ([{'id': [], 'host_id': 'a'}], 'registry host needs an id'),
    ([{'id': {}}, {'id': 'a'}], 'registry host needs an id'),
    ([{'id': 'a'}, {'host_id': 'a'}], 'duplicate registry host'),
])
def test_registry_keys_are_rust_validation_errors(registry, reason, spawnd):
    with pytest.raises(ValueError) as error:
        fleet.build_fleet(registry)
    assert str(error.value) == reason


def test_opaque_metadata_follows_rust_registry_indices(spawnd):
    first, second = object(), object()
    rows = fleet.build_fleet([{'host': 'z', 'caps': first},
                              {'id': 'a', 'host_id': 'z', 'caps': second}])['hosts']
    assert [row['host_id'] for row in rows] == ['a', 'z']
    assert rows[0]['caps'] is second and rows[1]['caps'] is first
    assert all('registry_index' not in row for row in rows)
