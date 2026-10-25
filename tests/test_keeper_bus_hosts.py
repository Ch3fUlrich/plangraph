"""BUS-REGISTRY-PERSIST: the keeper persists its host registry in ``bus_hosts``.

The in-keeper Registry loop writes the table through ``Store.upsert_bus_hosts``,
``GET /v1/hosts`` reads ``Store.list_bus_hosts`` joined with the current
inventory, and the rows survive a keeper restart: the persisted ``endpoint`` /
``last_seen`` are loaded back into the Host objects before the first probe while
health is forced to ``offline`` (a pre-restart ``healthy`` is never trusted).
``key_file`` never reaches the table or the response.

The PG cases run on a Postgres 16 server via ``PLANGRAPH_TEST_PG_DSN`` (or the
suite-wide ``PG_DSN``); they are skipped with a reason when neither is set.
"""
import json
import re
import threading

import pytest

from keeper_util import World
from plangraph.fleet_bus.discovery import Host, Registry
from plangraph.keeper import api, config, db as kdb, schema
from plangraph.keeper.store import Store

import os

DSN = os.environ.get("PLANGRAPH_TEST_PG_DSN") or os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(
    not DSN,
    reason="PLANGRAPH_TEST_PG_DSN (or PG_DSN) not set: no Postgres 16 test server")

HOST_FIELDS = ("host", "node_id", "key_id", "endpoint", "health", "last_seen", "updated_ts")
LAST_SEEN = 1_700_000_111.0


def row(host="host-a", node="node-1", key="ka", endpoint="http://host-a:8765",
        health="healthy", last_seen=LAST_SEEN):
    return {"host": host, "node_id": node, "key_id": key, "endpoint": endpoint,
            "health": health, "last_seen": last_seen}


def host_registry(tmp_path):
    key_file = tmp_path / "host-a.key"
    key_file.write_bytes(b"k" * 32)
    return Registry([Host("host-a", "node-1", "ka", key_file)]), key_file


def online_status():
    """A fake tailscale status: node-1 is online as host-a (a DNS name, never an IP)."""
    return {"Peer": {"node-1": {"Online": True, "DNSName": "host-a."}}}


def one_refresh(registry, status=None, probe=lambda endpoint: True, now=LAST_SEEN):
    """Run exactly one Registry.run iteration with a fake discover/probe."""
    stop = threading.Event()

    def discover():
        stop.set()
        return online_status() if status is None else status

    registry.run(stop, probe, interval=0, discover=discover, clock=lambda: now)


# ------------------------------------------------------------------ schema

def test_bus_hosts_table_declared_in_both_ddls():
    from plangraph.keeper import schema_pg
    assert "bus_hosts" in schema.SCHEMA
    assert "bus_hosts" in schema_pg.MIGRATIONS[-1].sql
    body = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?bus_hosts \(\n(.*?)\n\);",
                     schema.SCHEMA, re.S)
    assert body is not None, "bus_hosts must be created in the SQLite schema"
    cols = re.findall(r'^    "?(\w+)"? \w+', body.group(1), re.M)
    assert list(cols) == ["host", "node_id", "key_id", "endpoint", "health",
                          "last_seen", "updated_ts"]
    assert "key_file" not in body.group(1)


# -------------------------------------------------------- store: upsert/list

def test_upsert_and_list_roundtrip_sqlite(tmp_path):
    w = World(tmp_path / "lite")
    try:
        w.store.upsert_bus_hosts([row(), row("host-b", "node-2", "kb",
                                             endpoint=None, health="offline",
                                             last_seen=None)])
        rows = w.store.list_bus_hosts()
        assert [r["host"] for r in rows] == ["host-a", "host-b"]
        assert {name: rows[0][name] for name in HOST_FIELDS} == {
            "host": "host-a", "node_id": "node-1", "key_id": "ka",
            "endpoint": "http://host-a:8765", "health": "healthy",
            "last_seen": LAST_SEEN, "updated_ts": rows[0]["updated_ts"]}
        assert rows[1]["endpoint"] is None and rows[1]["last_seen"] is None
        assert isinstance(rows[0]["updated_ts"], int)
        # an upsert updates in place: same host, new state, still one row
        w.store.upsert_bus_hosts([row(health="unhealthy", last_seen=LAST_SEEN + 5)])
        rows = w.store.list_bus_hosts()
        assert [r["host"] for r in rows] == ["host-a", "host-b"]
        assert rows[0]["health"] == "unhealthy" and rows[0]["last_seen"] == LAST_SEEN + 5
    finally:
        w.close()


@needs_pg
def test_upsert_and_list_roundtrip_pg(tmp_path):
    w = World(tmp_path / "pg", backend="postgres", dsn=DSN)
    try:
        w.store.upsert_bus_hosts([row(), row("host-b", "node-2", "kb",
                                             endpoint=None, health="offline",
                                             last_seen=None)])
        rows = w.store.list_bus_hosts()
        assert [r["host"] for r in rows] == ["host-a", "host-b"]
        assert rows[0]["endpoint"] == "http://host-a:8765"
        assert rows[0]["health"] == "healthy" and rows[0]["last_seen"] == LAST_SEEN
        w.store.upsert_bus_hosts([row(health="offline", last_seen=LAST_SEEN + 5)])
        rows = w.store.list_bus_hosts()
        assert len(rows) == 2 and rows[0]["last_seen"] == LAST_SEEN + 5
    finally:
        w.close()


def test_upsert_never_stores_key_material(tmp_path):
    w = World(tmp_path / "lite")
    try:
        w.store.upsert_bus_hosts([dict(row(), key_file=str(tmp_path / "host-a.key"))])
        rows = w.store.list_bus_hosts()
        assert list(rows[0]) == list(HOST_FIELDS)
        assert "key_file" not in rows[0]
        columns = [r[1] for r in w.conn.execute("PRAGMA table_info(bus_hosts)")]
        assert "key_file" not in columns
    finally:
        w.close()


# ------------------------------------------------------- registry: persistence

def test_registry_restore_never_trusts_a_pre_restart_healthy(tmp_path):
    registry, _ = host_registry(tmp_path)
    registry.restore([
        dict(row(), endpoint="http://host-a:8765", health="healthy",
             last_seen=LAST_SEEN),
        row("ghost-host", "node-9", "kg"),           # not in the inventory: ignored
    ])
    host = registry.hosts["host-a"]
    assert host.endpoint == "http://host-a:8765"
    assert host.last_seen == LAST_SEEN
    assert host.health == "offline"                  # forced: resolve() must not trust it
    with pytest.raises(LookupError):
        registry.resolve("host-a")


def test_on_refresh_error_does_not_kill_the_loop(tmp_path, caplog):
    registry, _ = host_registry(tmp_path)
    calls = []

    def on_refresh(hosts):
        calls.append(list(hosts))
        raise RuntimeError("persistence exploded")

    stop = threading.Event()
    rounds = []

    def discover():
        rounds.append(1)
        if len(rounds) == 3:
            stop.set()
        return {}

    with caplog.at_level("ERROR", logger="plangraph.fleet_bus"):
        registry.run(stop, lambda endpoint: False, interval=0,
                     discover=discover, clock=lambda: 1.0, on_refresh=on_refresh)
    assert len(calls) == 3                       # the loop kept refreshing after the error
    assert [h.host for h in calls[0]] == ["host-a"]
    assert "RuntimeError" in caplog.text and "persistence exploded" in caplog.text


# ---------------------------------------------------------- keeper: GET /hosts

def make_inventory(tmp_path, monkeypatch, hosts=("host-a",)):
    """A bus inventory with one key file per host; returns (inventory path, key files)."""
    entries, keys = [], {}
    for index, name in enumerate(hosts):
        key_file = tmp_path / f"{name}.key"
        key_file.write_bytes(bytes([ord("k") + index]) * 32)
        keys[name] = key_file
        entries.append({"host": name, "node_id": f"node-{index + 1}",
                        "key_id": f"k{name}", "key_file": str(key_file)})
    inv_file = tmp_path / "inventory.json"
    inv_file.write_text(json.dumps({"hosts": entries, "port": 8765}))
    monkeypatch.setenv("PLANGRAPH_BUS_INVENTORY", str(inv_file))
    return inv_file, keys


def start_keeper(store, tmp_path):
    cfg = config.Config({"host": "127.0.0.1", "port": 0}, tmp_path)
    server = api.make_server(store, cfg, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread


def stop_keeper(server, thread):
    if getattr(server, "bus_stop", None) is not None:
        server.bus_stop.set()
        if server.bus_thread is not None:
            server.bus_thread.join(timeout=5)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def get_hosts(server, token):
    from test_keeper_api import call, jbody
    status, _, raw = call(server, "/v1/hosts", token=token)
    return status, jbody(raw), raw


def test_hosts_survive_a_keeper_restart(tmp_path, monkeypatch):
    """The real keeper server, two runs on one database: last_seen survives, health resets."""
    make_inventory(tmp_path, monkeypatch)
    lite = tmp_path / "run"
    lite.mkdir()
    w = World(lite)

    # ---- run 1: the keeper's registry loop persists one refresh -----------------
    srv1, thread1 = start_keeper(w.store, tmp_path)
    assert srv1.bus_registry is not None, "the server must build the registry from the inventory"
    srv1.bus_stop.set()                     # the test owns the probe from here
    srv1.bus_thread.join(timeout=5)
    one_refresh(srv1.bus_registry, probe=lambda endpoint: True)
    persisted = w.store.list_bus_hosts()
    assert [r["host"] for r in persisted] == ["host-a"]
    assert persisted[0]["last_seen"] == LAST_SEEN
    assert persisted[0]["health"] == "healthy"
    stop_keeper(srv1, thread1)
    w.close()

    # ---- run 2: a fresh server on the same database ----------------------------
    conn = kdb.open_db(w.path, create=False)
    store = Store(conn, clock=w.clock)
    srv2, thread2 = start_keeper(store, tmp_path)
    srv2.bus_stop.set()                     # before the first probe
    srv2.bus_thread.join(timeout=5)
    try:
        status, data, raw = get_hosts(srv2, w.tok("router"))
        assert status == 200
        assert [h["host"] for h in data["hosts"]] == ["host-a"]
        host = data["hosts"][0]
        assert host["last_seen"] == LAST_SEEN      # the persisted last_seen, kept as is
        assert host["health"] == "offline"         # never a pre-restart 'healthy'
        assert host["persisted"] is True
        assert "key_file" not in host
    finally:
        stop_keeper(srv2, thread2)
        conn.close()


def test_get_hosts_serves_the_inventory_only(tmp_path, monkeypatch):
    make_inventory(tmp_path, monkeypatch, hosts=("host-a",))
    lite = tmp_path / "lite"
    lite.mkdir()
    w = World(lite)
    srv, thread = start_keeper(w.store, tmp_path)
    srv.bus_stop.set()                      # the test owns the table from here
    srv.bus_thread.join(timeout=5)
    try:
        w.store.upsert_bus_hosts([row(), row("ghost-host", "node-9", "kg")])
        assert [r["host"] for r in w.store.list_bus_hosts()] == ["ghost-host", "host-a"]
        status, data, _ = get_hosts(srv, w.tok("router"))
        assert status == 200
        assert [h["host"] for h in data["hosts"]] == ["host-a"]   # ghost is not served
        host = data["hosts"][0]
        assert host["key_id"] == "khost-a"
        assert host["endpoint"] == "http://host-a:8765"
        assert host["health"] == "healthy" and host["last_seen"] == LAST_SEEN
        assert host["persisted"] is True
    finally:
        stop_keeper(srv, thread)
        w.close()


def test_no_key_file_in_the_table_or_the_response(tmp_path, monkeypatch):
    _, keys = make_inventory(tmp_path, monkeypatch, hosts=("host-a",))
    key_path = str(keys["host-a"])
    lite = tmp_path / "lite"
    lite.mkdir()
    w = World(lite)
    srv, thread = start_keeper(w.store, tmp_path)
    srv.bus_stop.set()                      # the test owns the refresh from here
    srv.bus_thread.join(timeout=5)
    try:
        one_refresh(srv.bus_registry, probe=lambda endpoint: True)
        raw_rows = [dict(r) for r in w.conn.execute("SELECT * FROM bus_hosts")]
        assert raw_rows, "the refresh must have written the row"
        for row_dict in raw_rows:
            assert "key_file" not in row_dict
            assert all(key_path != value for value in row_dict.values())
        status, _, raw = get_hosts(srv, w.tok("router"))
        assert status == 200
        text = raw.decode("utf-8")
        assert "key_file" not in text
        assert key_path not in text
    finally:
        stop_keeper(srv, thread)
        w.close()
