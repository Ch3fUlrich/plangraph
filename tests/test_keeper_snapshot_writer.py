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
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / "contract"))

import contract_harness as harness  # noqa: E402
import pytest  # noqa: E402
from contract_world import make_plan, ok, snapshot  # noqa: E402

from keeper_util import World, seed  # noqa: E402
from plangraph.keeper import api, config, snapshot_io, snapshot_writer  # noqa: E402


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


def _writer(srv, debounce=0.05, max_delay=0.4):
    w = snapshot_writer.SnapshotWriter(srv, debounce, max_delay)
    w.start()
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


def test_a_burst_of_writes_is_one_rebuild(fake):
    w = _writer(fake)
    try:
        for _ in range(50):
            _mark(w)
        assert _until(lambda: fake.builds)
        time.sleep(0.3)
        assert len(fake.builds) == 1
    finally:
        w.stop()


def test_a_steady_stream_cannot_starve_the_file(fake):
    w = _writer(fake, debounce=0.2, max_delay=0.5)
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.6:      # a mark every 50 ms: never quiet for 200 ms
            _mark(w)
            time.sleep(0.05)
        assert len(fake.builds) >= 2, fake.builds
        assert fake.builds[0] - t0 < 0.5 + 0.3   # first rebuild by max_delay (+ scheduling slack)
    finally:
        w.stop()


def test_flush_levels_the_file_before_returning(fake):
    w = _writer(fake, debounce=30, max_delay=60)
    try:
        _mark(w)
        w.flush()
        assert len(fake.builds) == 1
        w.flush()                                # nothing pending: no rebuild
        assert len(fake.builds) == 1
    finally:
        w.stop()


def test_stop_flushes_a_pending_change(fake):
    w = _writer(fake, debounce=30, max_delay=60)
    _mark(w)
    w.stop()
    assert len(fake.builds) == 1 and not w.is_alive()


def test_a_failed_build_is_retried_and_does_not_kill_the_thread(fake):
    fake.fail = 2
    w = _writer(fake, debounce=0.05, max_delay=0.1)
    try:
        _mark(w)
        assert _until(lambda: fake.builds, 5), "never retried after a failed build"
        assert w.is_alive()
    finally:
        w.stop()


def _server(tmp_path, world, debounce):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path), "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0", "snapshot_debounce_s": debounce,
        "snapshot_max_delay_s": 5}), encoding="utf-8")
    os.chmod(path, 0o600)
    server = api.make_server(world.store, config.load_config(path), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server


def test_server_close_flushes_the_last_committed_write(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    server = _server(tmp_path, world, debounce=30)   # the debounce alone would never fire
    snap = tmp_path / "plan.snapshot.json"
    try:
        from test_keeper_api import call
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
        from test_keeper_api import call
        call(server, "/v1/claim", method="POST", body={"key": "demo:A"}, token=world.tok("l1main"))
        assert (tmp_path / "plan.snapshot.json").exists()      # written before the response
    finally:
        server.shutdown()
        server.server_close()
        world.close()


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
            # parses JSON and verifies the signature; the documented sig/body gap (new sig, old
            # body) is retried by the reader, and a loaded runner can starve it for a moment,
            # so only a read that never verifies in 5 tries (or any torn JSON) counts as torn.
            for attempt in range(5):
                try:
                    snapshot_io.read_snapshot(path, pub)
                    break
                except snapshot_io.SignError:
                    time.sleep(0.01)
                except Exception as exc:                  # noqa: BLE001 - torn JSON etc.
                    bad.append(repr(exc))
                    break
            else:
                bad.append("never verified in 5 tries")
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
