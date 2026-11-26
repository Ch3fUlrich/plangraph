"""KEEPER-SNAPSHOT-PG: the debounced snapshot writer — coalescing, bounded staleness, and that a
committed write never ends up missing from (or torn in) ``plan.snapshot.json``.

Mutations killed: no flush on ``server_close`` (shutdown test), no startup rebuild (SIGKILL
test), a rebuild per mark (coalescing), no ``max_delay_s`` cap (starvation), a thread that dies
on one failed build (retry), a non-atomic file replace (torn-read stress).
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / "contract"))

import contract_harness as harness  # noqa: E402
import pytest  # noqa: E402
from contract_world import make_plan, ok, snapshot  # noqa: E402

from keeper_util import World, seed  # noqa: E402
from plangraph.keeper import api, config, snapshot_io, snapshot_writer  # noqa: E402
from plangraph.keeper.errors import ConnectionLost  # noqa: E402
from test_keeper_api import call, make_cfg  # noqa: E402


class FakeServer:
    """Just what ``SnapshotWriter`` touches: a lock and a counted ``write_snapshot``."""

    def __init__(self):
        self.lock = threading.RLock()
        self.system = object()
        self.builds = []
        self.fail = 0


@pytest.fixture
def fake(monkeypatch):
    srv = FakeServer()

    def build(server, principal):
        if srv.fail:
            srv.fail -= 1
            raise OSError("disk full")
        srv.builds.append(time.monotonic())

    monkeypatch.setattr(snapshot_writer, "write_snapshot", build)
    return srv


def _writer(srv, debounce=0.05, max_delay=0.4, *, request=None):
    w = snapshot_writer.SnapshotWriter(srv, debounce, max_delay)
    w.start()
    if request is not None:
        request.addfinalizer(w.stop)
    return w


def _mark(w):
    with w.server.lock:
        w.mark()


def _until(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_a_burst_of_writes_is_one_rebuild(fake, request):
    w = _writer(fake, request=request)
    for _ in range(50):
        _mark(w)
    assert _until(lambda: fake.builds)
    time.sleep(0.3)
    assert len(fake.builds) == 1


def test_a_steady_stream_cannot_starve_the_file(fake, request):
    w = _writer(fake, debounce=0.2, max_delay=0.5, request=request)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.6:      # a mark every 50 ms: never quiet for 200 ms
        _mark(w)
        time.sleep(0.05)
    assert len(fake.builds) >= 2, fake.builds
    assert fake.builds[0] - t0 < 0.5 + 0.3   # first rebuild by max_delay (+ scheduling slack)


def test_flush_levels_the_file_before_returning(fake, request):
    w = _writer(fake, debounce=30, max_delay=60, request=request)
    _mark(w)
    w.flush()
    assert len(fake.builds) == 1
    w.flush()                                # nothing pending: no rebuild
    assert len(fake.builds) == 1


def test_stop_flushes_a_pending_change(fake):
    w = _writer(fake, debounce=30, max_delay=60)
    _mark(w)
    w.stop()
    assert len(fake.builds) == 1 and not w.is_alive()


def test_build_failures_are_logged_and_a_streak_raises_one_alarm(fake, capsys, monkeypatch):
    fake.fail = 4
    fake.read_path = True
    tick, build = [0], snapshot_writer.write_snapshot
    monkeypatch.setattr(snapshot_writer, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    def slow_build(*args):
        tick[0] += 2
        build(*args)
    monkeypatch.setattr(snapshot_writer, "write_snapshot", slow_build)
    w = snapshot_writer.SnapshotWriter(fake, 1, 3)
    _mark(w)
    for attempt in range(5):
        tick[0] = w._due()
        w._attempt()
        assert w._next == tick[0] + 2 and w._written == (attempt == 4)
    err = capsys.readouterr().err
    assert fake.builds and err.count("snapshot build failed") == 4 and "OSError" in err
    assert err.count("ALARM") == 1 and err.count("outcome=error elapsed_ms=2000 lock_hold_ms=0") == 4
    assert "snapshot build recovered" in err and not w.alarm and w.failures == 0


def test_alarm_flag_is_up_while_the_streak_lasts(fake, capsys):
    fake.fail = 10**6
    w = _writer(fake, debounce=0.02, max_delay=0.05)
    try:
        _mark(w)
        assert _until(lambda: w.alarm, 5) and w.failures >= snapshot_writer.ALARM_AFTER
    finally:
        fake.fail = 0
        w.stop()


def test_a_failed_build_is_retried_and_does_not_kill_the_thread(fake, request):
    fake.fail = 2
    w = _writer(fake, debounce=0.05, max_delay=0.1, request=request)
    _mark(w)
    assert _until(lambda: fake.builds, 5), "never retried after a failed build"
    assert w.is_alive()


def _server(tmp_path, world, debounce):
    cfg = make_cfg(tmp_path, world)
    cfg.snapshot_debounce_s, cfg.snapshot_max_delay_s = debounce, 5
    server = api.make_server(world.store, cfg, port=0, log_timing=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server


def test_server_close_flushes_the_last_committed_write(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    server = _server(tmp_path, world, debounce=30)   # the debounce alone would never fire
    snap = tmp_path / "plan.snapshot.json"
    try:
        status = call(server, "/v1/claim", method="POST", body={"key": "demo:A"},
                      token=world.tok("l1main"))[0]
        assert status == 200 and not snap.exists(), "the response must not wait for the file"
        server.shutdown()
        server.server_close()
        data = json.loads(snap.read_bytes())
        item = next(i for i in data["items"] if i["key"] == "demo:A")
        assert item["state"] == "claimed"
    finally:
        world.close()


def test_synchronous_mode_is_unchanged_when_debounce_is_zero(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    server = _server(tmp_path, world, debounce=0)
    try:
        assert server.snapshot_writer is None
        call(server, "/v1/claim", method="POST", body={"key": "demo:A"}, token=world.tok("l1main"))
        assert (tmp_path / "plan.snapshot.json").exists()      # written before the response
    finally:
        server.shutdown()
        server.server_close()
        world.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_committed_capture_allows_write_and_readers_at_one_epoch(tmp_path, monkeypatch, backend):
    if backend == "postgres" and not os.environ.get("PG_DSN"):
        pytest.skip("PG_DSN not set")
    _paused_capture(tmp_path, monkeypatch, backend)


def test_pg_heldout_whoami_claim_during_paused_snapshot(tmp_path, monkeypatch):
    if not os.environ.get("PG_DSN") and os.environ.get("PLANGRAPH_REQUIRE_PG_HELDOUT") != "1":
        pytest.skip("PG_DSN not set")
    assert os.environ.get("PG_DSN"), "the required PostgreSQL fixture needs PG_DSN"
    _paused_capture(tmp_path, monkeypatch, "postgres")


@pytest.mark.parametrize("failure", [None, "capture", "publish"])
def test_pending_mark_and_flush_survive_capture_and_publish_failure(tmp_path, monkeypatch, failure):
    _paused_capture(tmp_path, monkeypatch, "sqlite", pause=0, failure=failure)


def _paused_capture(tmp_path, monkeypatch, backend, pause=10, failure=None):
    world = World(tmp_path, backend=backend)
    seed(world, [("A", []), ("B", [])])
    server = _server(tmp_path, world, debounce=0.02)
    entered, release, flushing = (threading.Event() for _ in range(3))
    writer = server.snapshot_writer
    real, publish, notify = type(world.store)._all_items, snapshot_io._publish, writer._cond.notify_all
    captures, published, transitions = [], [], []
    def items(view):
        rows = real(view)
        if threading.current_thread() is server.snapshot_writer and not captures:
            captures.append(view.conn)
            entered.set()
            assert release.wait(15)
            if failure == "capture":
                raise OSError("capture failed")
        return rows

    def graph(snap):
        return snap["head_seq"], snap["aliases"], {i["key"]: (i["state"], i["evidence"]) for i in snap["items"]}

    def record(path, snap, key):
        if failure == "publish" and threading.current_thread() is writer:
            raise OSError("publish failed")
        publish(path, snap, key)
        published.append(graph(snap))

    def record_pending():
        transitions.append((writer._written, writer._first, writer._last))
        notify()
    monkeypatch.setattr(type(world.store), "_all_items", items)
    monkeypatch.setattr(snapshot_io, "_publish", record)
    monkeypatch.setattr(writer._cond, "notify_all", record_pending)
    def live():
        return json.loads(call(server, "/v1/snapshot", token=world.tok("reader"))[2])
    def flush():
        assert not server.lock._is_owned()
        flushing.set()
        writer.flush()
    with closing(world), server, ExitStack() as shutdown:
        shutdown.callback(server.shutdown)
        before = live()
        with ThreadPoolExecutor(max_workers=1) as pool, ExitStack() as unblock:
            unblock.callback(release.set)
            _mark(writer)
            assert entered.wait(3)
            start = time.monotonic()
            for path in ("whoami", "show/demo:A?fmt=json", "ready?fmt=json", "snapshot"):
                assert pool.submit(call, server, "/v1/" + path, token=world.tok("reader")).result(1)[0] == 200
            claim = pool.submit(call, server, "/v1/claim", method="POST", token=world.tok("l1main"), body={"key": "demo:A"})
            assert claim.result(1)[0] == 200 and time.monotonic() - start < 1
            assert captures[0] is not world.conn and hasattr(captures[0], "schema") == (backend == "postgres")
            with server.lock:
                world.conn.execute("INSERT INTO aliases(old_key,new_key,ts,actor,reason) VALUES(?,?,?,?,?)",
                                   ("demo:OLD", "demo:A", world.store._iso(world.clock()), "router", "fixture"))
                world.store.add_evidence_router(world.p("router"), "demo:A", "decision", {"id": "D-1"})
            assert not release.wait(max(0, pause - (time.monotonic() - start)))
            with server.lock:
                future = pool.submit(flush)
                assert flushing.wait(1) and writer._build.locked()
                release.set()
                future.result(2)
        (_, oldest, _), (_, _, newer), (written, pending, _), _ = transitions
        assert (written, pending) == ((0, oldest) if failure else (1, newer))
        after = live()
        assert published == ([] if failure else [graph(before)]) + [graph(after)]
        assert after["head_seq"] > before["head_seq"] and after["aliases"] == {"demo:OLD": "demo:A"}
        assert graph(after)[2]["demo:A"][1]


@pytest.mark.parametrize("failure", ["unavailable", "shared", "wrong_source", "wrong_backend", "wrong_schema", "publish"])
def test_synchronous_capture_failure_never_falls_back_to_shared_store(tmp_path, monkeypatch, failure):
    from unittest.mock import Mock
    if failure in ("wrong_backend", "wrong_schema") and not os.environ.get("PG_DSN"):
        pytest.skip("PG_DSN not set")
    world = World(tmp_path, backend="postgres" if failure == "wrong_schema" else "sqlite")
    config = SimpleNamespace(db=world.path, snapshot=tmp_path / "snapshot.json")
    server = api.KeeperHTTPServer(("127.0.0.1", 0), world.store, config)
    real, closed = server.read_store, []

    @contextmanager
    def reader(**kwargs):
        if failure == "unavailable":
            raise RuntimeError("reader unavailable")
        with real(**kwargs) as view:
            closed.append(view.conn)
            yield world.store if failure == "shared" else view

    monkeypatch.setattr(server, "read_store", reader)
    if failure.startswith("wrong"):
        wrong = World(tmp_path / "wrong", backend="sqlite" if failure == "wrong_source" else "postgres")
        server.read_path, server.read_schema = wrong.path, getattr(wrong.conn, "schema", None)
        wrong.close()
    if failure == "publish":
        monkeypatch.setattr(snapshot_io, "_publish", Mock(side_effect=RuntimeError("publish failed")))
    writer = snapshot_writer.SnapshotWriter(server, 1, 2)
    writer.mark()
    with closing(world), server:
        with pytest.raises(RuntimeError):
            writer.flush()
        assert not config.snapshot.exists() and writer._written == 0 and writer._first is not None
        assert server.snapshot_publication.requested > server.snapshot_publication.latest
        for conn in closed:
            with pytest.raises((sqlite3.ProgrammingError, ConnectionLost)):
                conn.execute("SELECT 1")


def test_snapshot_config_keys_are_validated(tmp_path):
    path = tmp_path / "keeper.json"
    for bad in (-1, "x", True):
        path.write_text(json.dumps({"snapshot_debounce_s": bad}), encoding="utf-8")
        os.chmod(path, 0o600)
        with pytest.raises(config.ConfigError):
            config.load_config(path)


# ----------------------------------------------------------------- daemon, real processes
def _live(kp, path):
    live = snapshot(kp)
    for _ in range(200):
        snap = json.loads(path.read_bytes())
        if snap["head_seq"] >= live["head_seq"]:
            return live, snap
        time.sleep(0.05)
    return live, snap


@pytest.fixture
def daemon(tmp_path):
    if harness.from_env() is not None:
        pytest.skip("needs a local keeper")
    kp = harness.start_local(tmp_path / "k", signing=True)
    yield kp
    kp.stop()


def test_sigterm_right_after_a_write_leaves_the_file_level_and_signed(daemon):
    plan = make_plan(daemon)                      # the response is out; the file may lag
    daemon.proc.send_signal(signal.SIGTERM)
    assert daemon.proc.wait(timeout=20) == 0
    daemon.proc = None
    path = daemon.dir / "plan.snapshot.json"
    keys = {i["key"] for i in json.loads(path.read_bytes())["items"]}
    assert plan.key in keys
    assert daemon.admin("verify-snapshot").returncode == 0


def test_sigkill_loses_no_committed_write_and_startup_rebuilds_the_file(daemon):
    plan = make_plan(daemon)
    daemon.proc.send_signal(signal.SIGKILL)       # the debounced rebuild may never have run
    daemon.proc.wait(timeout=20)
    port = int(daemon.url.rsplit(":", 1)[1])
    daemon.proc = __import__("subprocess").Popen(
        [sys.executable, "-m", "plangraph.keeper.daemon", "--config", str(daemon.config),
         "--host", "127.0.0.1", "--port", str(port)], env=harness._env(),
        stdout=__import__("subprocess").DEVNULL, stderr=__import__("subprocess").PIPE)
    for _ in range(400):
        try:
            if daemon.get("/v1/health").status == 200:
                break
        except OSError:
            pass
        time.sleep(0.05)
    live = snapshot(daemon)
    assert plan.key in {i["key"] for i in live["items"]}                 # committed: still there
    on_disk = json.loads((daemon.dir / "plan.snapshot.json").read_bytes())
    assert on_disk["head_seq"] == live["head_seq"]                       # and the file caught up
    assert daemon.admin("verify-snapshot").returncode == 0


def test_a_reader_never_sees_a_torn_or_unverifiable_snapshot(daemon):
    path, pub = daemon.dir / "plan.snapshot.json", daemon.dir / "snap.pub"
    make_plan(daemon)
    stop, bad, reads = threading.Event(), [], [0]

    def reader():
        while not stop.is_set():
            try:
                snapshot_io.read_snapshot(path, pub)      # production retry: 2 tries, one short pause
            except Exception as exc:                      # noqa: BLE001 - torn JSON or no verify
                bad.append(repr(exc))
            reads[0] += 1

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(25):
            make_plan(daemon)
    finally:
        _live(daemon, path)
        stop.set()
        thread.join()
    assert reads[0] > 20 and not bad, bad[:3]


def test_startup_snapshot_is_written_under_the_server_lock(tmp_path, monkeypatch):
    _startup_snapshot(tmp_path, monkeypatch, "sqlite", legacy=True)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_startup_snapshot_uses_committed_capture(tmp_path, monkeypatch, backend):
    if backend == "postgres" and not os.environ.get("PG_DSN"):
        pytest.skip("PG_DSN not set")
    _startup_snapshot(tmp_path, monkeypatch, backend)


def _startup_snapshot(tmp_path, monkeypatch, backend, legacy=False):
    from plangraph.keeper import daemon
    world = World(tmp_path, backend=backend)
    seed(world, [("A", [])])
    cfg = make_cfg(tmp_path, world)
    cfg.db_options["backend"] = backend
    make, capture, publish = api.make_server, snapshot_io._capture, snapshot_io._write
    seen = []

    def server(*args, **kwargs):
        srv = make(*args, **kwargs)
        srv.read_path = None if legacy else srv.read_path
        return srv

    def read(srv, view, who):
        seen.append(view.conn)
        assert srv.lock._is_owned() == legacy and (view.conn is srv.store.conn) == legacy
        return capture(srv, view, who)

    def write(*args):
        error = sqlite3.ProgrammingError if backend == "sqlite" else ConnectionLost
        with pytest.raises(error):
            seen[0].execute("SELECT 1")
        publish(*args)

    monkeypatch.setattr(api, "make_server", server)
    monkeypatch.setattr(snapshot_io, "_capture", read)
    if not legacy:
        monkeypatch.setattr(snapshot_io, "_write", write)
    world.close()
    daemon.run(cfg, ready=lambda srv: signal.raise_signal(signal.SIGINT))
    assert seen and "demo:A" in {item["key"] for item in snapshot_io.read_snapshot(cfg.snapshot)["items"]}


def test_show_answers_during_a_fleet_build_of_7000_records(tmp_path, monkeypatch, capsys):
    from plangraph import fleet_snapshot

    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    for n in range(7000):
        path = tmp_path / "runs" / f"run-{n}" / "record.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"run_id": f"run-{n}", "status": "running",
                                   "host": "fixture", "attempts": ["large record"]}))
    world = World(tmp_path, backend="sqlite")
    seed(world, [("A", [])])
    server = _server(tmp_path, world, debounce=0.02)
    entered, unblocked = threading.Event(), threading.Event()
    observed = []

    def project(registry, samples=None, runs=None):
        observed.append((len(runs), set(runs[0])))
        entered.set()
        assert unblocked.wait(5)
        return {"hosts": [], "running_agents": len(runs)}

    monkeypatch.setattr(fleet_snapshot, "build_fleet", project)
    try:
        server.snapshot_writer.mark()
        assert entered.wait(4)
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                start = time.monotonic()
                response = pool.submit(call, server, "/v1/show/demo:A?fmt=json", token=world.tok("reader"))
                assert response.result(timeout=1)[0] == 200
                assert time.monotonic() - start < 1
            finally:
                unblocked.set()
        server.flush_snapshot()
        assert observed == [(7000, {"run_id", "host", "status"})]
        timing = next(line for line in capsys.readouterr().err.splitlines() if "writer_timing" in line)
        parts = dict(field.split("=", 1) for field in timing.split() if "=" in field)
        assert int(parts["hold_ms"]) < 2500
        assert int(parts["fleet_ms"]) > 0
    finally:
        unblocked.set()
        server.shutdown()
        server.server_close()
        world.close()


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not os.environ.get("PG_DSN"), reason="PG_DSN not set"))])
def test_plan_reads_see_the_committed_state_while_a_close_write_is_paused(tmp_path, backend):
    world = World(tmp_path, backend=backend)
    seed(world, [("A", [])])
    server = _server(tmp_path, world, debounce=30)
    entered, release = threading.Event(), threading.Event()

    def close():
        with server.lock, world.store._write():
            world.conn.execute("UPDATE items SET state='done' WHERE key='demo:A'")
            entered.set()
            assert release.wait(30)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            write = pool.submit(close)
            try:
                assert entered.wait(2)
                requests = [pool.submit(call, server, "/v1/" + path, token=world.tok("reader"))
                            for path in ("show/demo:A?fmt=json", "ready?fmt=json", "snapshot")]
                show, ready, snap = [request.result(timeout=1) for request in requests]
                assert show[0] == ready[0] == snap[0] == 200
                assert json.loads(show[2])["state"] == json.loads(snap[2])["items"][0]["state"] == "open"
                assert "demo:A" in json.dumps(json.loads(ready[2])["items"])
            finally:
                release.set()
            write.result(timeout=5)
        after = call(server, "/v1/show/demo:A?fmt=json", token=world.tok("reader"))
        assert json.loads(after[2])["state"] == "done"
        def read_count():
            with server.lock:
                return world.conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE actor=? AND action IN ('show','ready','snapshot')",
                    ("reader",)).fetchone()[0]
        assert _until(lambda: read_count() == 4)
    finally:
        server.shutdown()
        server.server_close()
        world.close()
