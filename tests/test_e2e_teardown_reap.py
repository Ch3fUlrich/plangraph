import os
import importlib
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).parent / "e2e"))
import rtp1_local_util as u
pytestmark = pytest.mark.skipif(os.name != "posix" or not Path('/proc/self').exists(),
                                reason="requires POSIX sessions and Linux /proc")


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.fixture
def children():
    pids = []
    yield pids
    for pid in pids:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    end = time.monotonic() + 2
    while any(alive(pid) for pid in pids) and time.monotonic() < end:
        time.sleep(.01)
    assert not any(alive(pid) for pid in pids)


def child(pid_dir, children, ignore=False):
    pid_dir.mkdir(exist_ok=True)
    code = ("import os,signal,time,pathlib; "
            f"signal.signal(signal.SIGTERM, {'signal.SIG_IGN' if ignore else 'signal.SIG_DFL'}); "
            f"pathlib.Path({str(pid_dir)!r}, str(os.getpid())).write_text(str(os.getpid())); "
            "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    children.append(p.pid)
    end = time.monotonic() + 2
    while not (pid_dir / str(p.pid)).exists():
        assert time.monotonic() < end
        time.sleep(.01)
    return p


def test_detached_child(tmp_path, children):
    parent = subprocess.Popen(["sleep", "60"], start_new_session=True)
    children.append(parent.pid)
    p = child(tmp_path / "pids", children)
    try:
        os.killpg(parent.pid, signal.SIGTERM)
        parent.wait(timeout=2)
        assert alive(p.pid)
        assert u.reap_recorded(tmp_path / "pids", grace_s=.1) == [p.pid]
        assert not alive(p.pid)
    finally:
        p.kill() if alive(p.pid) else None
        p.wait(timeout=2)


def test_term_ignorer(tmp_path, children):
    p = child(tmp_path, children, ignore=True)
    try:
        start = time.monotonic()
        assert u.reap_recorded(tmp_path, grace_s=.1) == [p.pid]
        assert time.monotonic() - start < 2.1
        assert not alive(p.pid)
    finally:
        p.kill() if alive(p.pid) else None
        p.wait(timeout=2)


def test_stale_and_missing(tmp_path, children):
    p = subprocess.Popen(["sleep", "60"])
    children.append(p.pid)
    try:
        (tmp_path / str(p.pid)).write_text(str(p.pid))
        (tmp_path / "2147483647").write_text("2147483647")
        assert u.reap_recorded(tmp_path, grace_s=.01) == []
        assert alive(p.pid)
    finally:
        p.kill()
        p.wait(timeout=2)


def wait_recorded_pid(pid_dir):
    end = time.monotonic() + 2
    while True:
        for record in pid_dir.iterdir():
            # The shell's redirect creates the entry before echo writes the PID.
            # reap_recorded skips empty/partial records, so wait for publication.
            if record.read_text() == f"{record.name}\n":
                return int(record.name)
        assert time.monotonic() < end, "fake opencode did not publish its PID"
        time.sleep(.01)


@pytest.mark.parametrize("pending", ["", "12"])
def test_wait_recorded_pid_waits_for_complete_record(tmp_path, monkeypatch, pending):
    record = tmp_path / "123"
    record.write_text(pending)
    # Drive publication at the next poll, without a scheduling-dependent delay.
    monkeypatch.setattr(sys.modules[__name__], "time", SimpleNamespace(
        monotonic=time.monotonic, sleep=lambda _: record.write_text("123\n")))
    assert wait_recorded_pid(tmp_path) == 123
    assert record.read_text() == "123\n", "directory entry is not a published PID"


def test_exited_worker(tmp_path, children, monkeypatch):
    monkeypatch.setattr(u, "FAKE_OPENCODE", u.FAKE_OPENCODE + "exec sleep 60\n")
    bin_dir = tmp_path / "different bin"
    pid_dir = u.write_fake_opencode(bin_dir)
    binary = tmp_path / "worker"
    binary.write_text(f'#!/bin/sh\nsetsid "{bin_dir}/opencode" &\n')
    binary.chmod(0o700)
    worker = u.WorkerProc(str(binary), dict(os.environ, PG_JAIL="off",
                          PG_FAKE_PID_DIR=str(pid_dir)), tmp_path / "worker.log")
    try:
        worker.proc.wait(timeout=2)
        pid = wait_recorded_pid(pid_dir)
        children.append(pid)
        assert worker.pid_dir == pid_dir
        worker.stop()
        assert not alive(pid)
    finally:
        worker.stop()


def test_straggler_does_not_abort(tmp_path, monkeypatch, caplog):
    records = [tmp_path / "101", tmp_path / "102"]
    for record in records:
        record.write_text(record.name)
    proc = MagicMock()
    proc.__truediv__.return_value = proc
    proc.read_text.return_value = "101 (fake) S"
    proc.read_bytes.return_value = str(tmp_path).encode()
    monkeypatch.setattr(u, "Path", lambda p: proc if str(p).startswith("/proc/") else Path(p))
    signals = []
    monkeypatch.setattr(u, "os", SimpleNamespace(name="posix", kill=lambda p, s: signals.append((p, s))))
    assert u.reap_recorded(tmp_path, grace_s=0) == []
    assert set(signals) == {(p, s) for p in (101, 102) for s in (signal.SIGTERM, signal.SIGKILL)}
    assert all(str(p) in caplog.text for p in (101, 102))


@pytest.mark.parametrize("group_error", [False, True])
def test_stop_reaps_after_timeout_or_group_error(tmp_path, monkeypatch, group_error):
    worker = u.WorkerProc.__new__(u.WorkerProc)
    worker.pid_dir, worker.out, worker.proc = tmp_path, MagicMock(), MagicMock()
    worker.proc.poll.return_value = None
    worker.proc.wait.side_effect = [subprocess.TimeoutExpired("worker", 10), None]
    killpg = MagicMock(side_effect=PermissionError if group_error else None)
    reap = MagicMock()
    monkeypatch.setattr(u, "os", SimpleNamespace(name="posix", killpg=killpg))
    assert os.killpg is not killpg
    monkeypatch.setattr(u, "reap_recorded", reap)
    if group_error:
        with pytest.raises(PermissionError):
            worker.stop()
    else:
        worker.stop()
        assert killpg.call_args_list[-1].args[1] == signal.SIGKILL
    reap.assert_called_once_with(tmp_path)
    worker.out.close.assert_called_once()


def test_real_fake_records_and_behaves(tmp_path):
    bin_dir = tmp_path / "bin with spaces"
    u.write_fake_opencode(bin_dir)
    for prompt in ("question", u.ANSWER_TOKEN):
        p = subprocess.Popen([str(bin_dir / "opencode"), "run", "-m", "fake", prompt],
                             cwd=tmp_path, stdout=subprocess.PIPE, text=True)
        stdout, _ = p.communicate(timeout=2)
        assert p.returncode == 0
        assert (bin_dir / "pids" / str(p.pid)).read_text().strip() == str(p.pid)
        assert ("PG_QUESTION:" if prompt == "question" else "wrote out.txt") in stdout
    assert (tmp_path / u.CARD_FILE).read_text() == "done\n"


def test_fake_rejects_changed_shebang(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "FAKE_OPENCODE", u.FAKE_OPENCODE.replace("#!/bin/sh", "#!/bin/bash"))
    with pytest.raises(AssertionError, match="shebang"):
        u.write_fake_opencode(tmp_path / "bin")


def test_worker_does_not_guess_pid_directory(tmp_path, monkeypatch):
    popen = MagicMock()
    popen.return_value.poll.return_value = 0
    monkeypatch.setattr(u.subprocess, "Popen", popen)
    reap = MagicMock()
    monkeypatch.setattr(u, "reap_recorded", reap)
    worker = u.WorkerProc("unused", {"PG_JAIL": "off"}, tmp_path / "worker.log")
    try:
        assert worker.pid_dir is None
        assert "PG_FAKE_PID_DIR" not in popen.call_args.kwargs["env"]
    finally:
        worker.stop()
    reap.assert_not_called()


def test_fake_missing_pid_directory_is_nonfatal(tmp_path):
    bin_dir = tmp_path / "other bin"
    pid_dir = u.write_fake_opencode(bin_dir)
    pid_dir.rmdir()
    for prompt in ("question", u.ANSWER_TOKEN):
        result = subprocess.run(["sh", "-e", str(bin_dir / "opencode"), "run", prompt],
                                cwd=tmp_path, capture_output=True, text=True, timeout=2)
        assert result.returncode == 0
        assert ("PG_QUESTION:" if prompt == "question" else "wrote out.txt") in result.stdout
    assert (tmp_path / u.CARD_FILE).read_text() == "done\n"


def test_fixture_failed_wait_up_stops_worker(tmp_path, monkeypatch, children):
    rig_module = importlib.import_module("test_rtp1_local_e2e")
    worker = MagicMock()
    p = child(tmp_path / "pids", children)
    worker.wait_up.side_effect = RuntimeError("startup failed")
    worker.stop.side_effect = lambda: u.reap_recorded(tmp_path / "pids", grace_s=0)
    fake_u = MagicMock()
    fake_u.WorkerProc.return_value = worker
    for name, value in dict(u=fake_u, DevServer=MagicMock(), World=MagicMock(), seed=MagicMock()).items():
        monkeypatch.setattr(rig_module, name, value)
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            next(rig_module.rig.__wrapped__(tmp_path, monkeypatch, MagicMock(param="ready")))
        worker.stop.assert_called_once()
        assert not alive(p.pid)
    finally:
        p.kill() if alive(p.pid) else None
        p.wait(timeout=2)


def test_pid_lookup_error_is_not_failure(tmp_path, monkeypatch):
    class Unreadable:
        def glob(self, pattern):
            yield tmp_path / "stale"
            raise OSError("lookup failed")
    assert u.reap_recorded(Unreadable()) == []


def test_regression_tests_use_local_mocks_and_real_fixture():
    import inspect
    assert 'setattr(Path,' not in inspect.getsource(test_straggler_does_not_abort)
    assert 'setattr(u.time,' not in inspect.getsource(test_straggler_does_not_abort)
    assert '.__wrapped__(' in inspect.getsource(test_fixture_failed_wait_up_stops_worker)
