"""Tests for Windows-safe record write and read (SPAWN-WIN-PARITY S3)."""
import asyncio
import os
from pathlib import Path
import time
import pytest

from plangraph.routing_support import private_io
from plangraph.spawner.service import SpawnService
from plangraph.spawner import stall_watchdog


def test_atomic_write_retries_winerror_5(tmp_path, monkeypatch):
    """(a) os.replace raises PermissionError winerror 5 twice then succeeds:
    content written, no .record.json-* temp left, 3 calls.
    """
    target = tmp_path / "record.json"
    calls = []
    real_replace = os.replace

    def fake_replace(src, dst):
        calls.append((src, dst))
        if len(calls) < 3:
            exc = PermissionError("access denied")
            exc.winerror = 5
            raise exc
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(private_io.time, "sleep", lambda s: None)

    private_io.atomic_write_private(target, '{"status": "ok"}')
    assert target.read_text(encoding="utf-8") == '{"status": "ok"}'
    assert len(calls) == 3
    assert list(tmp_path.glob(".record.json-*")) == []


def test_atomic_write_winerror_5_exhausted(tmp_path, monkeypatch):
    """(b) winerror 5 forever: raises after 24 calls, no temp, under 5 s (patch time.sleep)."""
    target = tmp_path / "record.json"
    calls = []

    def fake_replace(src, dst):
        calls.append((src, dst))
        exc = PermissionError("access denied")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(os, "replace", fake_replace)
    slept = []
    monkeypatch.setattr(private_io.time, "sleep", slept.append)

    start = time.perf_counter()
    with pytest.raises(PermissionError) as excinfo:
        private_io.atomic_write_private(target, '{"status": "fail"}')
    elapsed = time.perf_counter() - start

    assert excinfo.value.winerror == 5
    assert len(calls) == 24
    assert len(slept) == 23
    assert elapsed < 5.0
    assert list(tmp_path.glob(".record.json-*")) == []


def test_atomic_write_retry_sleep_budget(tmp_path, monkeypatch):
    """Worst-case retry sleeps stay inside the I13 budget (SYNC_SAVE_BUDGET_S),
    exact call count.

    Covers the write alone and a save operation whose record read shares the
    synchronous stretch with the write (SpawnService.cancel): two read sleeps
    plus 23 write sleeps must still sum to at most SYNC_SAVE_BUDGET_S.
    """
    target = tmp_path / "record.json"
    calls = []

    def fake_replace(src, dst):
        calls.append((src, dst))
        exc = PermissionError("access denied")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(os, "replace", fake_replace)
    slept = []
    monkeypatch.setattr(private_io.time, "sleep", slept.append)

    with pytest.raises(PermissionError):
        private_io.atomic_write_private(target, '{"status": "budget"}')

    assert len(calls) == 24
    assert len(slept) == 23
    assert round(sum(slept), 6) <= private_io.SYNC_SAVE_BUDGET_S
    assert list(tmp_path.glob(".record.json-*")) == []

    # Combined save-path budget: one record read plus one record write in the
    # same synchronous stretch. The read hits winerror 5 twice then succeeds
    # on its third call; only the write retries winerror 5 to exhaustion.
    run_id = "run-budget1"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "record.json").write_text(
        '{"run_id": "run-budget1", "status": "completed"}', encoding="utf-8")

    reads = []
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == "record.json":
            reads.append(1)
            if len(reads) < 3:
                exc = PermissionError("sharing violation")
                exc.winerror = 5
                raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    calls.clear()
    slept.clear()

    with pytest.raises(PermissionError):
        asyncio.run(SpawnService(state=tmp_path).cancel(run_id))

    assert len(reads) == 3
    assert len(calls) == 24
    assert len(slept) == 25
    assert round(sum(slept), 6) <= private_io.SYNC_SAVE_BUDGET_S


def test_save_path_two_reads_write_sleep_budget(tmp_path, monkeypatch):
    """Two save-path record reads plus the record write stay in the budget.

    The review spawn save stretch reads the record twice before saving: the
    family lookup and the excluded-family check (service.py calls
    result(review_of, attempts=SAVE_READ_ATTEMPTS) in both places), with no
    await between the reads and the later save. Two read_text_retry calls at
    SAVE_READ_ATTEMPTS that exhaust on winerror 5, then atomic_write_private
    exhausting on winerror 5: 2 x 2 read sleeps plus 23 write sleeps must
    still sum to at most SYNC_SAVE_BUDGET_S.
    """
    source = tmp_path / "runs" / "run-reviewsrc" / "record.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"run_id": "run-reviewsrc"}', encoding="utf-8")
    target = tmp_path / "runs" / "run-newrecord" / "record.json"

    reads = []
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == "record.json":
            reads.append(1)
            exc = PermissionError("sharing violation")
            exc.winerror = 5
            raise exc
        return real_read_text(self, *args, **kwargs)

    writes = []

    def fake_replace(src, dst):
        writes.append((src, dst))
        exc = PermissionError("access denied")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(os, "replace", fake_replace)
    slept = []
    monkeypatch.setattr(private_io.time, "sleep", slept.append)

    for _ in range(2):
        with pytest.raises(PermissionError):
            private_io.read_text_retry(source, attempts=private_io.SAVE_READ_ATTEMPTS)
    with pytest.raises(PermissionError):
        private_io.atomic_write_private(target, '{"status": "budget"}')

    assert len(reads) == 6
    assert len(writes) == 24
    assert len(slept) == 27
    assert round(sum(slept), 6) <= private_io.SYNC_SAVE_BUDGET_S
    assert list(tmp_path.glob("runs/*/.record.json-*")) == []


def test_save_path_read_attempts_lowered(tmp_path, monkeypatch):
    """A save path's record read gives up after the lowered read budget.

    The read keeps raising winerror 5 while a successful read is available at
    call 5; the save path must stop at SAVE_READ_ATTEMPTS attempts and never
    let its write add retries on top.
    """
    run_id = "run-budget2"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "record.json").write_text(
        '{"run_id": "run-budget2", "status": "completed"}', encoding="utf-8")

    reads = []
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == "record.json":
            reads.append(1)
            if len(reads) < 5:
                exc = PermissionError("sharing violation")
                exc.winerror = 5
                raise exc
        return real_read_text(self, *args, **kwargs)

    writes = []
    real_replace = os.replace

    def fake_replace(src, dst):
        writes.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(os, "replace", fake_replace)
    slept = []
    monkeypatch.setattr(private_io.time, "sleep", slept.append)

    with pytest.raises(PermissionError):
        asyncio.run(SpawnService(state=tmp_path).cancel(run_id))

    assert len(reads) == 3
    assert writes == []
    assert round(sum(slept), 6) <= private_io.SYNC_SAVE_BUDGET_S


def test_atomic_write_permission_error_no_winerror(tmp_path, monkeypatch):
    """(c) PermissionError without winerror: 1 call, raises."""
    target = tmp_path / "record.json"
    calls = []

    def fake_replace(src, dst):
        calls.append((src, dst))
        raise PermissionError("posix permission denied")

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(PermissionError):
        private_io.atomic_write_private(target, '{"status": "posix"}')
    assert len(calls) == 1
    assert list(tmp_path.glob(".record.json-*")) == []


def test_atomic_write_winerror_2_raises_at_once(tmp_path, monkeypatch):
    """(d) winerror 2 raises at once."""
    target = tmp_path / "record.json"
    calls = []

    def fake_replace(src, dst):
        calls.append((src, dst))
        exc = PermissionError("error 2")
        exc.winerror = 2
        raise exc

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(PermissionError) as excinfo:
        private_io.atomic_write_private(target, '{"status": "err"}')
    assert excinfo.value.winerror == 2
    assert len(calls) == 1
    assert list(tmp_path.glob(".record.json-*")) == []


def test_result_succeeds_after_simulated_winerror_5(tmp_path, monkeypatch):
    """(e) result() succeeds after 2 simulated winerror-5 opens (patch through the helper)."""
    run_id = "run-test123"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    record_file = run_dir / "record.json"
    record_file.write_text('{"run_id": "run-test123", "status": "completed"}', encoding="utf-8")

    calls = []
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == "record.json":
            calls.append(1)
            if len(calls) < 3:
                exc = PermissionError("sharing violation")
                exc.winerror = 5
                raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    service = SpawnService(state=tmp_path)
    result = service.result(run_id)
    assert result == {"run_id": "run-test123", "status": "completed"}
    assert len(calls) == 3


def test_append_private_without_fchmod(tmp_path, monkeypatch):
    """(f) append_private with os.fchmod removed (monkeypatch delattr) writes, mode 0600."""
    target = tmp_path / "appended.txt"
    monkeypatch.delattr(os, "fchmod", raising=False)

    private_io.append_private(target, "first\n")
    private_io.append_private(target, "second\n")
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"
    if hasattr(os, "chmod") and os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_read_text_retry_helper(tmp_path, monkeypatch):
    """read_text_retry retries winerror 5 and 32, raises on other errors."""
    read_text_retry = getattr(private_io, "read_text_retry")
    f = tmp_path / "hello.txt"
    f.write_text("content", encoding="utf-8")

    calls = []
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == "hello.txt":
            calls.append(1)
            if len(calls) == 1:
                exc = PermissionError("winerror 5")
                exc.winerror = 5
                raise exc
            if len(calls) == 2:
                exc = PermissionError("winerror 32")
                exc.winerror = 32
                raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert read_text_retry(f) == "content"
    assert len(calls) == 3


def test_stall_watchdog_read_runs_retries_winerror_5(tmp_path, monkeypatch):
    """read_runs retries transient winerror 5 without marking record as bad."""
    run_dir = tmp_path / "runs" / "run-w1"
    run_dir.mkdir(parents=True, exist_ok=True)
    record_file = run_dir / "record.json"
    record_file.write_text('{"run_id": "run-w1", "status": "completed"}', encoding="utf-8")

    calls = []
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == "record.json":
            calls.append(1)
            if len(calls) < 3:
                exc = PermissionError("sharing violation")
                exc.winerror = 5
                raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    runs = stall_watchdog.read_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-w1"
    assert len(calls) == 3
    assert not (tmp_path / "watchdog" / "bad-records.json").exists()
