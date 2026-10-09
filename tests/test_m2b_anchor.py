"""M2B-ANCHOR (D-789 D1): anchor log writer, start check, socket service, daily tag.

HERMETIC (D-852): an autouse fixture puts stub ``systemctl``/``docker``/``ssh``/``curl``
first on PATH (each exits 97 and leaves a marker), every socket is a temp Unix socket,
every repo/remote a temp directory; ``test_guard_live_tools_are_stubbed`` fails if a
real live tool is reachable from the tests.

Mutations killed: an older or forked record appended, the start check skipped or using
the oldest anchor, a symlinked log followed, a done transition not anchored, an anchor
failure crashing the keeper, anchord importing the package (it must run as one file).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper import anchor, anchord, daemon

ROOT = Path(__file__).resolve().parents[1]
LIVE_TOOLS = ("systemctl", "docker", "ssh", "curl")


@pytest.fixture(autouse=True)
def stub_live_tools(tmp_path, monkeypatch):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in LIVE_TOOLS:
        path = stubs / tool
        path.write_text(f"#!/bin/sh\necho called > {stubs}/{tool}.CALLED\nexit 97\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    yield stubs
    called = [t for t in LIVE_TOOLS if (stubs / f"{t}.CALLED").exists()]
    assert not called, f"live tool invoked from the anchor code: {called}"


def test_guard_live_tools_are_stubbed(stub_live_tools):
    for tool in LIVE_TOOLS:
        assert shutil.which(tool) == str(stub_live_tools / tool), f"real {tool} reachable"


def rec(seq, head=None, reason="done"):
    return {"v": 1, "seq": seq, "head": head or f"{seq:064x}", "ts": "2026-10-09T10:00:00Z",
            "reason": reason}


# ------------------------------------------------------------------ anchord
def test_append_is_monotonic(tmp_path):
    log = tmp_path / "anchor.log"
    assert anchord.append(log, rec(5)) == "ok"
    assert anchord.append(log, rec(5)) == "ok duplicate"
    with pytest.raises(anchord.AnchorRefused, match="older"):
        anchord.append(log, rec(4))
    with pytest.raises(anchord.AnchorRefused, match="different head"):
        anchord.append(log, rec(5, head="f" * 64))
    assert anchord.append(log, rec(9)) == "ok"
    assert [json.loads(l)["seq"] for l in log.read_text().splitlines()] == [5, 9]


@pytest.mark.parametrize("bad", [
    b"not json", b'{"v":1}', json.dumps({**rec(1), "extra": 1}).encode(),
    json.dumps({**rec(1), "seq": True}).encode(), json.dumps({**rec(1), "head": "X" * 64}).encode(),
    json.dumps({**rec(1), "reason": "rm -rf"}).encode(), b"x" * 5000])
def test_serve_one_refuses_malformed(tmp_path, bad):
    out = io.BytesIO()
    assert anchord.serve_one(tmp_path / "a.log", io.BytesIO(bad + b"\n"), out) == 1
    assert out.getvalue().startswith(b"refused:")
    assert not (tmp_path / "a.log").exists() or (tmp_path / "a.log").read_text() == ""


def test_symlinked_log_is_refused(tmp_path):
    real = tmp_path / "real.log"
    real.write_text("")
    (tmp_path / "a.log").symlink_to(real)
    out = io.BytesIO()
    line = json.dumps(rec(1)).encode() + b"\n"
    assert anchord.serve_one(tmp_path / "a.log", io.BytesIO(line), out) == 1
    assert real.read_text() == ""


def test_anchord_runs_as_one_standalone_file(tmp_path):
    """The operator copies only anchord.py: it must not import the plangraph package."""
    copy = tmp_path / "lib" / "anchord.py"
    copy.parent.mkdir()
    shutil.copy(ROOT / "src/plangraph/keeper/anchord.py", copy)
    log = tmp_path / "anchor.log"
    run = subprocess.run([sys.executable, "-I", str(copy), "--log", str(log)],
                         input=json.dumps(rec(3)).encode() + b"\n", capture_output=True,
                         timeout=30, cwd=tmp_path, env={"PATH": os.environ["PATH"]})
    assert run.returncode == 0 and run.stdout == b"ok\n"
    assert json.loads(log.read_text())["seq"] == 3


# ------------------------------------------------------------- keeper side
@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _head(w):
    return w.conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()


def file_cfg(tmp_path):
    return {"anchor_file": str(tmp_path / "anchor.log")}


def test_start_check_refuses_rollback_and_fork(w, tmp_path):
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    assert anchor.check_start(w.conn, cfg) is None                 # empty log: nothing to check
    seq, head = _head(w)
    anchord.append(cfg["anchor_file"], rec(seq, head, "start"))
    assert anchor.check_start(w.conn, cfg)["seq"] == seq
    anchord.append(cfg["anchor_file"], rec(seq + 5, "a" * 64))     # anchor ahead of the DB
    with pytest.raises(anchor.AnchorError, match="behind"):
        anchor.check_start(w.conn, cfg)


def test_start_check_refuses_a_fork(w, tmp_path):
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    seq, _ = _head(w)
    anchord.append(cfg["anchor_file"], rec(seq, "b" * 64))
    with pytest.raises(anchor.AnchorError, match="fork"):
        anchor.check_start(w.conn, cfg)


def test_anchorer_records_start_done_and_hourly(w, tmp_path):
    (key,) = seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    clock = [1000.0]
    a = anchor.Anchorer(cfg, clock=lambda: clock[0])
    assert a.record(w.conn, "start") == "ok"
    assert a.tick(w.conn) is None                                    # nothing new
    to_claimed(w, key)                                               # not a done transition
    assert a.tick(w.conn) is None
    clock[0] += anchor.HOURLY_S
    assert a.tick(w.conn) == "ok"                                    # hourly while the head moves
    records, bad = anchor.read_records(cfg["anchor_file"])
    assert [r["reason"] for r in records] == ["start", "hourly"] and bad == 0
    assert anchor.verify(w.conn, cfg["anchor_file"]) == []


def test_done_transition_is_anchored_on_the_next_tick(w, tmp_path):
    from keeper_util import to_in_review
    (key,) = seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    a = anchor.Anchorer(cfg, clock=lambda: 1000.0)
    a.record(w.conn, "start")
    to_in_review(w, key)
    w.store.verdict(w.p("reviewer"), key, True, "ok")
    assert w.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()[0] == "done"
    assert a.tick(w.conn) == "ok"
    assert anchor.latest(cfg["anchor_file"])["reason"] == "done"
    assert anchor.latest(cfg["anchor_file"])["seq"] == _head(w)[0]


def test_verify_flags_mismatch_ahead_order_and_garbage(w, tmp_path):
    seed(w, [("A", [])])
    seq, head = _head(w)
    log = tmp_path / "anchor.log"
    lines = [rec(seq, head), rec(1, "c" * 64), rec(seq + 3, "d" * 64)]
    log.write_text("\n".join(json.dumps(r) for r in lines) + "\nnot json\n")
    codes = {p["code"] for p in anchor.verify(w.conn, log)}
    assert codes == {"anchor_mismatch", "anchor_order", "anchor_ahead", "anchor_malformed"}


# ------------------------------------------------------------ socket mode
@pytest.fixture
def service(tmp_path):
    """A temp Unix socket served by anchord.serve_one, as pganchor@.service would."""
    sock_path = str(tmp_path / "anchor.sock")
    log = tmp_path / "svc" / "anchor.log"
    log.parent.mkdir()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(4)
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn, conn.makefile("rb") as rf, conn.makefile("wb") as wf:
                anchord.serve_one(log, rf, wf)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield {"anchor_socket": sock_path, "anchor_log": str(log)}
    stop.set()
    t.join(timeout=2)
    srv.close()


def test_socket_mode_appends_and_refuses_older(w, service):
    seed(w, [("A", [])])
    seq, head = _head(w)
    assert anchor.submit(service, rec(seq, head, "start")) == "ok"
    with pytest.raises(anchor.AnchorError, match="older"):
        anchor.submit(service, rec(seq - 1, head))
    assert anchor.check_start(w.conn, service)["seq"] == seq


def test_unreachable_service_is_logged_once_not_raised(w, tmp_path):
    seed(w, [("A", [])])
    err = io.StringIO()
    cfg = {"anchor_socket": str(tmp_path / "missing.sock"), "anchor_log": str(tmp_path / "x.log")}
    a = anchor.Anchorer(cfg, err=err)
    assert a.record(w.conn, "start") is None
    assert a.record(w.conn, "start") is None
    assert err.getvalue().count("anchor not written") == 1


# ------------------------------------------------------------- daemon start
def test_daemon_refuses_a_rolled_back_db(tmp_path, capsys):
    world = World(tmp_path)
    seed(world, [("A", [])])
    seq, head = _head(world)
    world.close()
    log = tmp_path / "anchor.log"
    anchord.append(log, rec(seq + 10, "e" * 64))
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(tmp_path / "keeper.db"), "bind": "127.0.0.1:0",
                               "snapshot": str(tmp_path / "plan.json"), "anchor_file": str(log)}))
    os.chmod(cfg, 0o600)
    assert daemon.main(["--config", str(cfg)]) == 1
    assert "REFUSED by the anchor log" in capsys.readouterr().err


def test_append_only_flag_is_reported(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("")
    assert anchor.append_only(log) in (False, None)
    assert anchor.append_only(tmp_path / "missing") is None


# ---------------------------------------------------------------- daily tag
def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_anchor_tag_tags_once_per_day_and_pushes_to_a_local_remote(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import anchor_tag
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    repo = tmp_path / "data"
    _git("init", "-q", str(repo), cwd=tmp_path)
    (repo / "f").write_text("x")
    _git("add", "f", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "c", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    log = tmp_path / "anchor.log"
    anchord.append(log, rec(7))
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    assert anchor_tag.main(["--repo", str(repo), "--anchor-log", str(log), "--push"], now=now) == 0
    msg = _git("tag", "-l", "--format=%(contents)", "anchor-20261009", cwd=repo).stdout
    assert json.loads(msg.strip())["seq"] == 7
    assert "anchor-20261009" in _git("tag", "-l", cwd=origin).stdout
    assert anchor_tag.main(["--repo", str(repo), "--anchor-log", str(log)], now=now) == 0   # exists


def test_anchor_tag_without_records_fails(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import anchor_tag
    assert anchor_tag.main(["--repo", str(tmp_path), "--anchor-log", str(tmp_path / "none")]) == 1
