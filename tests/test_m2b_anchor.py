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
    """PATH = stubs + a bin dir holding ONLY the tools these tests need (no real live tool)."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in LIVE_TOOLS:
        path = stubs / tool
        path.write_text(f"#!/bin/sh\necho called > {stubs}/{tool}.CALLED\nexit 97\n")
        path.chmod(0o755)
    needed = tmp_path / "bin"
    needed.mkdir()
    for tool in ("git", "sh", "env"):
        real = shutil.which(tool)
        if real:
            (needed / tool).symlink_to(real)
    (needed / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{stubs}:{needed}")
    yield stubs
    called = [t for t in LIVE_TOOLS if (stubs / f"{t}.CALLED").exists()]
    assert not called, f"live tool invoked from the anchor code: {called}"


def test_guard_live_tools_are_stubbed(stub_live_tools):
    """Fails if ANY PATH entry offers a real systemctl/docker/ssh/curl (D-852)."""
    for tool in LIVE_TOOLS:
        hits = [os.path.join(d, tool) for d in os.environ["PATH"].split(os.pathsep)
                if os.path.exists(os.path.join(d, tool))]
        assert hits == [str(stub_live_tools / tool)], f"real {tool} reachable: {hits}"


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
def service(tmp_path, monkeypatch):
    """A temp Unix socket served by anchord.serve_one, as pganchor@.service would.

    The temp log belongs to the test user; production refuses that (see
    test_socket_mode_refuses_a_log_the_keeper_can_write), so the ownership check is
    neutralised here only."""
    monkeypatch.setattr(anchor, "_keeper_can_write", lambda path: False)
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


# ------------------------------------------------------ round-1 seat findings
def test_forged_higher_anchor_cannot_whitewash_a_rewritten_chain(w, tmp_path):
    """Every anchor must match, not only the newest (seat S-V1/V2)."""
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    seq, head = _head(w)
    anchord.append(cfg["anchor_file"], rec(seq, head))
    # the chain is rewritten at seq (simulated by an anchor that disagrees there) and a
    # forged newer anchor names the rewritten head: the older anchor still refuses.
    anchord.append(cfg["anchor_file"], rec(seq + 1, "9" * 64))
    with pytest.raises(anchor.AnchorError):
        anchor.check_start(w.conn, cfg)
    log = tmp_path / "forged.log"
    log.write_text(json.dumps(rec(seq, "1" * 64)) + "\n" + json.dumps(rec(seq, head)) + "\n")
    with pytest.raises(anchor.AnchorError, match="fork or rewrite"):
        anchor.check_start(w.conn, {"anchor_file": str(log)})


def test_garbage_lines_do_not_hide_a_valid_anchor(w, tmp_path):
    seed(w, [("A", [])])
    seq, _ = _head(w)
    log = tmp_path / "a.log"
    log.write_bytes(b"not json\n" + json.dumps(rec(seq + 4, "a" * 64)).encode() + b"\n\xff\xfe\n")
    with pytest.raises(anchor.AnchorError, match="behind"):
        anchor.check_start(w.conn, {"anchor_file": str(log)})
    log.write_bytes(b"not json\n{\"v\":1\n")                         # garbage only
    with pytest.raises(anchor.AnchorError, match="no valid record"):
        anchor.check_start(w.conn, {"anchor_file": str(log)})


def test_torn_last_line_does_not_swallow_the_next_record(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(json.dumps(rec(5)).encode() + b"\n" + b'{"v":1,"seq":6')   # crash mid-write
    assert anchord.append(log, rec(7)) == "ok"
    records, bad = anchor.read_records(log)
    assert [r["seq"] for r in records] == [5, 7] and bad == 1


def test_non_ascii_record_is_refused(tmp_path):
    out = io.BytesIO()
    line = json.dumps({**rec(1), "ts": "2026-10-09T10:00:00Z\u00e9"}, ensure_ascii=False).encode("utf-8")
    assert anchord.serve_one(tmp_path / "a.log", io.BytesIO(line + b"\n"), out) == 1


_WRITER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import anchord
log, start = sys.argv[2], int(sys.argv[3])
ok = []
for seq in range(start, 200, 4):
    rec = {"v": 1, "seq": seq, "head": format(seq, "064x"), "ts": "2026-10-09T10:00:00Z", "reason": "done"}
    try:
        if anchord.append(log, rec) == "ok":
            ok.append(seq)
    except anchord.AnchorRefused:
        pass
print(json.dumps(ok))
"""


def test_concurrent_processes_never_lose_an_ok_or_tear_a_line(tmp_path):
    """Separate PROCESSES (flock is per open file): every 'ok' answer is in the log, in order."""
    log = tmp_path / "a.log"
    lib = str(ROOT / "src/plangraph/keeper")
    procs = [subprocess.Popen([sys.executable, "-I", "-c", _WRITER, lib, str(log), str(p)],
                              stdout=subprocess.PIPE) for p in range(1, 5)]
    oks = sorted(s for proc in procs for s in json.loads(proc.communicate(timeout=60)[0]))
    lines = log.read_text().splitlines()
    seqs = [json.loads(line)["seq"] for line in lines]          # every line parses: none torn
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert seqs == oks and len(oks) >= 4


def test_symlinked_anchor_log_fails_closed_at_start(w, tmp_path):
    seed(w, [("A", [])])
    real = tmp_path / "real.log"
    real.write_text("")
    link = tmp_path / "a.log"
    link.symlink_to(real)
    with pytest.raises(anchor.AnchorError, match="unreadable"):
        anchor.check_start(w.conn, {"anchor_file": str(link)})


def test_socket_mode_refuses_a_missing_log_or_no_log_path(w, tmp_path):
    seed(w, [("A", [])])
    with pytest.raises(anchor.AnchorError, match="missing"):
        anchor.check_start(w.conn, {"anchor_socket": str(tmp_path / "s.sock"),
                                    "anchor_log": str(tmp_path / "nope.log")})
    with pytest.raises(anchor.AnchorError, match="needs anchor_log"):
        anchor.check_start(w.conn, {"anchor_socket": str(tmp_path / "s.sock")})


def test_refusing_service_is_logged_once_and_backs_off(w, service):
    seed(w, [("A", [])])
    seq, head = _head(w)
    anchor.submit(service, rec(seq + 50, "c" * 64))          # service log now ahead: it refuses
    err = io.StringIO()
    clock = [5000.0]
    a = anchor.Anchorer(service, clock=lambda: clock[0], err=err)
    a.last_seq = 0
    assert a.record(w.conn, "start") is None
    assert "older" in err.getvalue()
    calls = []
    real_submit = anchor.submit
    anchor.submit = lambda cfg, r: calls.append(r) or real_submit(cfg, r)
    try:
        assert a.record(w.conn, "hourly") is None and calls == []   # backing off, no call
        clock[0] += anchor.RETRY_AFTER_S
        a.record(w.conn, "hourly")
        assert len(calls) == 1                                       # retried after the back-off
    finally:
        anchor.submit = real_submit
    assert err.getvalue().count("anchor not written") == 1


def _directives(path):
    return {line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def test_unit_files_keep_the_sandbox():
    sock = _directives(ROOT / "deploy/pganchor/pganchor.socket")
    svc = _directives(ROOT / "deploy/pganchor/pganchor@.service")
    assert {"Accept=yes", "SocketMode=0660", "SocketUser=pganchor", "SocketGroup=s",
            "MaxConnections=8", "ListenStream=/run/pganchor/anchor.sock"} <= sock
    assert {"User=pganchor", "Group=pganchor", "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/pganchor", "PrivateNetwork=yes", "NoNewPrivileges=yes",
            "RuntimeMaxSec=10", "CapabilityBoundingSet=", "ProtectHome=yes",
            "RestrictAddressFamilies=AF_UNIX",
            "ExecStart=/usr/bin/python3 -I /usr/local/lib/pganchor/anchord.py --log /var/lib/pganchor/anchor.log"} <= svc
    assert not any(line.startswith(("AmbientCapabilities", "User=root")) for line in svc)


def test_reaper_anchors_under_the_server_lock_after_its_writes(tmp_path):
    """The reaper calls tick only while holding the server lock, after reap + snapshot (committed)."""
    from types import SimpleNamespace
    from plangraph.keeper import api
    order = []
    lock = __import__("threading").RLock()

    class FakeAnchorer:
        def tick(self, conn):
            order.append(("tick", lock._is_owned()))
    store = SimpleNamespace(reap=lambda p: order.append(("reap", lock._is_owned())), conn=None)
    server = SimpleNamespace(lock=lock, store=store, system=None, anchorer=FakeAnchorer(),
                             config=SimpleNamespace(reap_interval=0.01, db=str(tmp_path / "k.db"),
                                                    snapshot=str(tmp_path / "s.json"), signing_key=None))
    api_write = api.write_snapshot
    api.write_snapshot = lambda srv, p: order.append(("snapshot", lock._is_owned()))
    try:
        reaper = api.Reaper(server)
        reaper.start()
        for _ in range(200):
            if any(o[0] == "tick" for o in order):
                break
            __import__("time").sleep(0.01)
        reaper.stop()
    finally:
        api.write_snapshot = api_write
    first = [o[0] for o in order][:3]
    assert first == ["reap", "snapshot", "tick"] and all(owned for _, owned in order[:3])


def test_silent_service_times_out_quickly(tmp_path):
    """A service that accepts but never answers costs <= ~1 s under the lock, then raises."""
    import time as _t
    path = str(tmp_path / "silent.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    try:
        start = _t.monotonic()
        with pytest.raises(anchor.AnchorError):
            anchor.submit({"anchor_socket": path, "anchor_log": str(tmp_path / "x")}, rec(1))
        assert _t.monotonic() - start < 3
    finally:
        srv.close()


def test_anchor_tag_pushes_only_its_own_tag(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import anchor_tag
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    repo = tmp_path / "data; touch PWNED"
    _git("init", "-q", str(repo), cwd=tmp_path)
    (repo / "f").write_text("x")
    _git("add", "f", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "c", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("tag", "unrelated", cwd=repo)
    log = tmp_path / "anchor.log"
    anchord.append(log, rec(3))
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    assert anchor_tag.main(["--repo", str(repo), "--anchor-log", str(log), "--push"], now=now) == 0
    tags = _git("tag", "-l", cwd=origin).stdout.split()
    assert tags == ["anchor-20261010"] and not (tmp_path / "PWNED").exists()


def test_record_slipped_in_after_the_start_check_is_refused_loudly(w, service):
    """Round-2 seat S-V1: the floor comes from check_start's single read, not a re-read."""
    seed(w, [("A", [])])
    Path(service["anchor_log"]).write_text("")                 # operator step A2: empty log exists
    last = anchor.check_start(w.conn, service)                 # empty service log: None
    anchor.submit(service, rec(999999, "a" * 64, "start"))      # forged after the check
    err = io.StringIO()
    a = anchor.Anchorer(service, err=err, last=last)
    assert a.last_seq == 0
    assert a.record(w.conn, "start") is None                    # refused by the service ...
    assert "older" in err.getvalue()                           # ... and reported, not silent
    with pytest.raises(anchor.AnchorError, match="behind"):     # next start refuses
        anchor.check_start(w.conn, service)


def test_socket_mode_refuses_a_log_the_keeper_can_write(w, tmp_path):
    """Round-3 seat T-V2: a pganchor log writable by the keeper's user is an install error."""
    seed(w, [("A", [])])
    log = tmp_path / "anchor.log"
    log.write_text("")                                      # owned by the test (= keeper) user
    with pytest.raises(anchor.AnchorError, match="writable by the keeper"):
        anchor.check_start(w.conn, {"anchor_socket": str(tmp_path / "s.sock"), "anchor_log": str(log)})


def test_short_writes_still_write_the_whole_record(tmp_path, monkeypatch):
    """Round-3 seat T-V3: os.write may return less than asked; the record must still be whole."""
    real_write = os.write
    monkeypatch.setattr(anchord.os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    log = tmp_path / "a.log"
    log.write_bytes(json.dumps(rec(1)).encode())             # torn tail, no newline
    assert anchord.append(log, rec(2)) == "ok"
    records, bad = anchor.read_records(log)
    assert [r["seq"] for r in records] == [1, 2] and bad == 0


def test_an_uncommitted_head_is_never_anchored(w, tmp_path):
    """Round-3 seat T-V6: another connection's open transaction is invisible to the anchorer."""
    import sqlite3
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    a = anchor.Anchorer(cfg, clock=lambda: 1000.0)
    a.record(w.conn, "start")
    committed, _ = _head(w)
    other = sqlite3.connect(w.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO events (ts, type, payload, prev_hash, hash) VALUES (?,?,?,?,?)",
                  ("2026-10-09T10:00:00Z", "closed_on_decision", "{}", "x", "f" * 64))
    try:
        a.clock = lambda: 1000.0 + anchor.HOURLY_S
        assert a.tick(w.conn) is None                            # head unchanged for this conn
        assert anchor.latest(cfg["anchor_file"])["seq"] == committed
    finally:
        other.execute("ROLLBACK")
        other.close()


# ------------------------------------------------------ round-4 seat findings
def test_rewritten_chain_with_a_forged_newer_anchor_is_refused(w, tmp_path):
    """Real rewrite: the event at the genuine anchor changes and a forged newer anchor matches the new tip."""
    import sqlite3
    seed(w, [("A", [])])
    seq, head = _head(w)
    log = tmp_path / "a.log"
    anchord.append(log, rec(seq, head))                          # genuine anchor
    raw = sqlite3.connect(w.path)
    raw.execute("DROP TRIGGER IF EXISTS trg_events_append_only_update")
    raw.execute("UPDATE events SET hash=? WHERE seq=?", ("e" * 64, seq))   # rewritten history
    raw.execute("INSERT INTO events (ts, type, payload, prev_hash, hash) VALUES (?,?,?,?,?)",
                ("2026-10-09T10:00:00Z", "closed_on_decision", "{}", "e" * 64, "d" * 64))
    raw.commit()
    raw.close()
    anchord.append(log, rec(seq + 1, "d" * 64))                  # forged newer anchor = new tip
    with pytest.raises(anchor.AnchorError, match="fork or rewrite"):
        anchor.check_start(w.conn, {"anchor_file": str(log)})


def test_socket_mode_refuses_a_writable_directory(w, tmp_path, monkeypatch):
    """A read-only log in a directory the keeper can write is still replaceable: refused."""
    seed(w, [("A", [])])
    d = tmp_path / "pganchor"
    d.mkdir()
    log = d / "anchor.log"
    log.write_text("")
    log.chmod(0o444)
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 0xFFFF)     # the log is "not ours"
    with pytest.raises(anchor.AnchorError, match="writable by the keeper"):
        anchor.check_start(w.conn, {"anchor_socket": str(tmp_path / "s.sock"), "anchor_log": str(log)})


def test_unit_files_have_each_key_directive_exactly_once():
    """systemd: the LAST directive wins, so a later override must not exist (seat T-V4)."""
    for name in ("pganchor.socket", "pganchor@.service"):
        lines = [l.strip().split("=", 1)[0] for l in (ROOT / "deploy/pganchor" / name).read_text().splitlines()
                 if "=" in l and not l.lstrip().startswith("#")]
        dup = {k for k in lines if lines.count(k) > 1}
        assert not dup, f"{name}: repeated directives {dup}"


def test_file_mode_deleted_log_after_anchors_is_refused(w, tmp_path):
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    a = anchor.Anchorer(cfg, clock=lambda: 1000.0)
    assert a.record(w.conn, "start") == "ok"
    Path(cfg["anchor_file"]).unlink()
    with pytest.raises(anchor.AnchorError, match="lost records"):
        anchor.check_start(w.conn, cfg)


# ------------------------------------------------------ round-5 seat findings
def test_file_mode_emptied_log_after_anchors_is_refused(w, tmp_path):
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    anchor.Anchorer(cfg, clock=lambda: 1000.0).record(w.conn, "start")
    Path(cfg["anchor_file"]).write_text("")
    with pytest.raises(anchor.AnchorError, match="lost records"):
        anchor.check_start(w.conn, cfg)


def test_invalid_utf8_record_is_refused_not_crashing(tmp_path):
    out = io.BytesIO()
    line = b'{"v":1,"seq":1,"head":"' + b"0" * 64 + b'","ts":"\xff","reason":"done"}\n'
    assert anchord.serve_one(tmp_path / "a.log", io.BytesIO(line), out) == 1
    assert out.getvalue().startswith(b"refused:")


def test_tick_with_an_unreachable_service_never_raises(w, tmp_path):
    (key,) = seed(w, [("A", [])])
    err = io.StringIO()
    clock = [1000.0]
    a = anchor.Anchorer({"anchor_socket": str(tmp_path / "missing.sock"),
                         "anchor_log": str(tmp_path / "x.log")}, clock=lambda: clock[0], err=err,
                        last=None)
    to_claimed(w, key)
    clock[0] += anchor.HOURLY_S
    assert a.tick(w.conn) is None and "anchor not written" in err.getvalue()


def test_tick_skips_while_the_same_connection_has_an_open_transaction(w, tmp_path):
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    clock = [1000.0]
    a = anchor.Anchorer(cfg, clock=lambda: clock[0])
    a.record(w.conn, "start")
    before = anchor.latest(cfg["anchor_file"])["seq"]
    w.conn.execute("BEGIN")
    try:
        w.conn.execute("INSERT INTO events (ts, type, payload, prev_hash, hash) VALUES (?,?,?,?,?)",
                       ("2026-10-09T10:00:00Z", "closed_on_decision", "{}", "x", "f" * 64))
        clock[0] += anchor.HOURLY_S
        assert a.tick(w.conn) is None
    finally:
        w.conn.execute("ROLLBACK")
    assert anchor.latest(cfg["anchor_file"])["seq"] == before


def test_hourly_tick_without_new_events_writes_nothing(w, tmp_path):
    seed(w, [("A", [])])
    cfg = file_cfg(tmp_path)
    clock = [1000.0]
    a = anchor.Anchorer(cfg, clock=lambda: clock[0])
    a.record(w.conn, "start")
    clock[0] += 2 * anchor.HOURLY_S
    assert a.tick(w.conn) is None
    assert len(anchor.read_records(cfg["anchor_file"])[0]) == 1


def test_anchor_log_that_is_a_directory_fails_closed(w, tmp_path, capsys):
    """Round-6 seat S-V5: a directory as the log raises AnchorError (daemon exits 1), never crashes."""
    seed(w, [("A", [])])
    d = tmp_path / "logdir"
    d.mkdir()
    with pytest.raises(anchor.AnchorError, match="unreadable"):
        anchor.check_start(w.conn, {"anchor_file": str(d)})
