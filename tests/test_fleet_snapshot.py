"""Hermetic fleet projection and persisted snapshot failure isolation."""
import json
import os
from types import SimpleNamespace

import pytest

from _spawnd import built_rust_binary, fleet_spawnd, require_spawnd
from plangraph import fleet_snapshot as fleet
from plangraph.keeper.snapshot_io import write_snapshot
from keeper_util import World
from plangraph.spawner.service import SpawnService

REGISTRY = [{"id": "a", "aliases": ["alias"], "class": "worker",
             "caps": {"max_runs": 4}}, {"id": "b"}]


@pytest.mark.parametrize('release', ['old', 'other-tree', 'current'])
def test_fleet_binary_must_match_current_sources(tmp_path, monkeypatch, release):
    import _spawnd

    target = tmp_path / 'cargo-target'
    source = tmp_path / 'checkout/rust/pg-spawnd/src/main.rs'
    source.parent.mkdir(parents=True)
    source.touch()
    os.utime(source, (200, 200))
    monkeypatch.setattr(_spawnd, '__file__', str(tmp_path / 'checkout/tests/_spawnd.py'))
    monkeypatch.setenv('CARGO_TARGET_DIR', str(target))

    def artifact(profile, built_from, timestamp):
        path = built_rust_binary('pg-spawnd', profile)
        path.parent.mkdir(parents=True)
        path.touch()
        os.utime(path, (timestamp, timestamp))
        path.with_name('pg-spawnd.d').write_text(f'{path}: {built_from}\n')
        return path

    release_binary = artifact('release', source if release != 'other-tree'
                              else tmp_path / 'other/rust/pg-spawnd/src/main.rs',
                              100 if release == 'old' else 300)
    if release == 'current':
        assert fleet_spawnd() == release_binary
    else:
        with pytest.raises(RuntimeError, match='stale pg-spawnd'):
            fleet_spawnd()
        debug_binary = artifact('debug', source, 300)
        assert fleet_spawnd() == debug_binary


@pytest.fixture
def spawnd(monkeypatch):
    binary = fleet_spawnd()
    monkeypatch.setattr(fleet, '_projection_binary', lambda: str(binary))
    return binary


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
    import subprocess
    home = tmp_path / 'home'
    monkeypatch.setattr(fleet.Path, 'home', lambda: home)
    monkeypatch.setattr(fleet, '__file__', str(tmp_path / 'src/plangraph/fleet_snapshot.py'))
    path_dir = tmp_path / 'system-bin'
    monkeypatch.setenv('PATH', str(path_dir))
    if ci is None:
        monkeypatch.delenv('CI', raising=False)
    else:
        monkeypatch.setenv('CI', ci)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('must not exec'))
    with pytest.raises(RuntimeError) as error:
        fleet.build_fleet([])
    reason = str(error.value)
    assert reason.startswith('pg-spawnd binary missing (build the release helper)')
    assert str(path_dir) in reason
    for directory in (home / '.local/bin', tmp_path / 'rust/target/release'):
        for name in ('pg-spawnd', 'pg-spawnd.exe'):
            assert str(directory / name) in reason
    outcome = pytest.fail.Exception if ci else pytest.skip.Exception
    with pytest.raises(outcome) as error:
        require_spawnd()
    assert str(error.value) == reason


@pytest.mark.parametrize('path_hit,installed,checkout,selected', [
    (True, True, 'pg-spawnd', 'path'),
    (False, True, 'pg-spawnd', 'installed'),
    (False, True, None, 'installed'),
    (False, False, 'pg-spawnd', 'checkout'),
    (False, False, 'pg-spawnd.exe', 'checkout'),
])
def test_projection_binary_lookup(tmp_path, monkeypatch, path_hit, installed, checkout, selected):
    import subprocess
    home = tmp_path / 'home'
    monkeypatch.setattr(fleet.Path, 'home', lambda: home)
    monkeypatch.setattr(fleet, '__file__', str(tmp_path / 'src/plangraph/fleet_snapshot.py'))
    path_dir = tmp_path / 'system-bin'
    monkeypatch.setenv('PATH', str(path_dir))
    name = 'pg-spawnd.exe' if os.name == 'nt' else 'pg-spawnd'
    binaries = {
        'path': path_dir / name,
        'installed': home / '.local/bin' / name,
        'checkout': tmp_path / 'rust/target/release' / (checkout or 'pg-spawnd'),
    }
    for location, present in [('path', path_hit), ('installed', installed), ('checkout', checkout)]:
        if present:
            binary = binaries[location]
            binary.parent.mkdir(parents=True)
            binary.touch(mode=0o755)

    def run(command, **kwargs):
        assert command == [str(binaries[selected]), 'fleet-snapshot']
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


def test_cargo_artifact_report_names_the_exact_binary():
    import json
    from _spawnd import artifact_executable
    lines = [json.dumps(dict(reason='compiler-artifact', target=dict(name='pg-spawnd', kind=['lib']),
                             executable=None)),
             'not json',
             json.dumps(dict(reason='compiler-artifact', target=dict(name='other', kind=['bin']),
                             executable='/t/other')),
             json.dumps(dict(reason='compiler-artifact', target=dict(name='pg-spawnd', kind=['bin']),
                             executable='/per-run/debug/pg-spawnd'))]
    assert artifact_executable(lines, 'pg-spawnd') == '/per-run/debug/pg-spawnd'
    assert artifact_executable(lines[:3], 'pg-spawnd') is None


def test_spawnd_build_ignores_shared_target_overrides():
    from _spawnd import TREE_TARGET, spawnd_build_command, spawnd_build_env
    env = spawnd_build_env({'CARGO_TARGET_DIR': '/shared', 'CARGO_BUILD_TARGET_DIR': '/shared',
                            'PATH': '/bin'})
    assert env == {'PATH': '/bin'}
    command = spawnd_build_command(False)
    assert command[command.index('--target-dir') + 1] == str(TREE_TARGET)
    assert '--release' not in command and '--release' in spawnd_build_command(True)


def test_spawnd_build_failure_fails_with_cargo_diagnostics(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    import _spawnd
    monkeypatch.setattr(_spawnd, '_BUILT', {})
    import json
    # Cargo's --message-format=json puts the rendered error on stdout, not stderr.
    stdout = json.dumps(dict(reason='compiler-message', message=dict(
        level='error', rendered='error[E0432]: unresolved import `missing`\n --> src/main.rs:1:5\n')))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=101, stdout=stdout + '\n{"reason":"build-finished","success":false}', stderr=''))
    with pytest.raises(pytest.fail.Exception, match=r'(?s)E0432.*src/main\.rs:1:5'):
        _spawnd.build_spawnd()


def test_fast_checks_rebuilds_after_cargo_leaves_a_stale_shared_binary(tmp_path, monkeypatch):
    import _spawnd

    root = tmp_path / 'checkout'
    source = root / 'rust/pg-spawnd/src/main.rs'
    source.parent.mkdir(parents=True)
    source.touch()
    os.utime(source, (200, 200))
    target = tmp_path / 'cargo-target'
    binary = target / 'debug/pg-spawnd'
    binary.parent.mkdir(parents=True)
    binary.touch()
    os.utime(binary, (100, 100))
    binary.with_suffix('.d').write_text(f'{binary}: {source}\n')
    monkeypatch.setattr(_spawnd, '__file__', str(root / 'tests/_spawnd.py'))
    monkeypatch.setenv('CARGO_TARGET_DIR', str(target))
    calls = []

    def rebuild(command, *, cwd):
        calls.append((command, cwd))
        os.utime(binary, (300, 300))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_spawnd.subprocess, 'run', rebuild)
    assert _spawnd.ensure_fleet_spawnd() == 0
    assert len(calls) == 1
    command, cwd = calls[0]
    assert command[:2] == ['cargo', 'rustc'] and cwd == root
    assert any(arg.startswith('metadata=') for arg in command)
    assert _spawnd.ensure_fleet_spawnd() == 0
    assert len(calls) == 1
