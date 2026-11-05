"""Timing vectors: deterministic clocks, local SQLite, no HTTP sockets or sleeps."""
import io
import json
import re
import threading
from types import SimpleNamespace

import pytest

from keeper_util import Clock, World, seed
from plangraph.keeper import anchor, api, snapshot_io, snapshot_writer, timing

PATTERN = re.compile(
    r"keeper: (request|writer|reaper|snapshot|changelog)_timing kind=(read|write|other)"
    r" wait_ms=\d+ hold_ms=\d+ build_ms=\d+ encode_ms=\d+ sign_ms=\d+"
    r" write_ms=\d+ tick_ms=\d+ record_ms=\d+ submit_ms=\d+")


@pytest.fixture
def meter(monkeypatch):
    clock, out = Clock(0), io.StringIO()
    monkeypatch.setattr(timing, "clock", clock)
    monkeypatch.setattr(timing, "sys", SimpleNamespace(stderr=out))
    return clock, out


def lines(out):
    result = out.getvalue().splitlines()
    assert result and all(PATTERN.fullmatch(line) for line in result)
    return result


def test_request_really_held_lock(meter):
    clock, out = meter
    lock = threading.Lock()
    lock.acquire()
    acquiring = threading.Event()

    class Lock:
        def acquire(self):
            acquiring.set()
            lock.acquire()

        def release(self):
            lock.release()

    def request():
        with timing.RequestTimer(Lock(), "read"):
            clock.advance(.010)

    thread = threading.Thread(target=request)
    thread.start()
    assert acquiring.wait(2)
    clock.advance(.300)
    lock.release()
    thread.join(2)
    assert not thread.is_alive()
    assert "wait_ms=300 hold_ms=10" in lines(out)[0]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_api_hold_includes_encode_write_and_no_payload(method, meter, monkeypatch, tmp_path):
    clock, out = meter
    sentinel = "PRIVATE_SENTINEL"
    world = World(tmp_path, backend="sqlite")
    try:
        handler = object.__new__(api.KeeperHandler)
        handler.path = f"/v1/{'show' if method == 'GET' else 'claim'}/{sentinel}?q={sentinel}"
        handler.server = SimpleNamespace(lock=threading.Lock(), store=world.store)
        handler._token = lambda: world.tok("reader")
        handler._read_body = lambda: {"key": sentinel, "text": sentinel}
        handler._read = lambda *args: ({"key": sentinel}, False)
        handler._write = lambda *args: {"key": sentinel}
        handler._snapshot = lambda *args: None
        responses = []

        def respond(status, payload):
            clock.advance(.125)  # encode
            responses.append((status, json.dumps(payload)))
            clock.advance(.125)  # write

        handler._json = respond
        handler._handle(method)
        assert responses == [(200, json.dumps({"key": sentinel}))]
        assert "wait_ms=0 hold_ms=250" in lines(out)[0]
        assert sentinel not in out.getvalue()
        out.close()
        handler._handle(method)
        assert responses[1] == responses[0]
        out = io.StringIO()
        timing.sys.stderr = out
        out.seek(0)
        out.truncate()
        with timing.RequestTimer(threading.Lock(), sentinel):
            clock.advance(1)
        assert out.getvalue() == ""  # kind is closed, never echoed
    finally:
        world.close()


def test_threshold_only_read_write_and_finally(meter):
    clock, out = meter
    for kind in ("read", "write", "other"):
        with timing.RequestTimer(threading.Lock(), kind):
            clock.advance(.249)
    assert len(lines(out)) == 1
    assert "kind=other" in lines(out)[0]
    with pytest.raises(ValueError, match="original"):
        with timing.RequestTimer(threading.Lock()):
            raise ValueError("original")
    assert len(lines(out)) == 2


def test_snapshot_bytes_writer_parts_and_closed_stderr(meter, tmp_path, monkeypatch):
    clock, out = meter
    world = World(tmp_path, backend="sqlite")
    try:
        seed(world, [("A", [])])
        server = SimpleNamespace(store=world.store, system=world.p("system"),
                                 lock=threading.Lock(), config=SimpleNamespace(
                                     snapshot=tmp_path / "snapshot.json", signing_key=None))
        original = snapshot_io._atomic_write

        def write(path, body):
            clock.advance(.020)
            original(path, body)

        monkeypatch.setattr(snapshot_io, "_atomic_write", write)
        writer = snapshot_writer.SnapshotWriter(server, 0, 0)
        assert writer._write_locked()
        expected = server.config.snapshot.read_bytes()
        assert any("writer_timing" in line and "write_ms=20" in line for line in lines(out))
        assert any("snapshot_timing" in line for line in lines(out))
        out.close()
        snapshot_io.write_snapshot(server, server.system)
        assert server.config.snapshot.read_bytes() == expected
        monkeypatch.setattr(timing, "_emit", lambda *a: (_ for _ in ()).throw(RuntimeError()))
        assert writer._write_locked()
        assert server.config.snapshot.read_bytes() == expected
        def broken_write(*args):
            raise OSError("original write")
        monkeypatch.setattr(snapshot_io, "_atomic_write", broken_write)
        with pytest.raises(OSError, match="original write"):
            snapshot_io.write_snapshot(server, server.system)
        with pytest.raises(ValueError, match="original"):
            with timing.RequestTimer(server.lock):
                raise ValueError("original")
    finally:
        world.close()


def test_reaper_tick_record_submit_and_changelog(meter, tmp_path, monkeypatch):
    clock, out = meter
    world = World(tmp_path, backend="sqlite")
    try:
        seed(world, [("A", [])])
        def append(*args):
            clock.advance(.030)
            return "ok"
        monkeypatch.setattr(anchor.anchord, "append", append)
        anchorer = anchor.Anchorer({"anchor_file": str(tmp_path / "anchor")},
                                  clock=lambda: 10000, last=None)
        assert anchorer.tick(world.conn) == "ok"
        line = next(line for line in lines(out) if "reaper_timing" in line)
        assert "tick_ms=30 record_ms=30 submit_ms=30" in line
        world.store._changelog(0)
        assert "changelog_timing" in lines(out)[-1]
    finally:
        world.close()
