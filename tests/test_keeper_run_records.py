"""Snapshot run reads preserve unknown evidence and never repair files."""
import pytest

from plangraph.keeper.run_records import read_run_records


def test_missing_and_empty_runs_are_distinct(tmp_path):
    assert read_run_records(tmp_path) is None
    (tmp_path / "runs").mkdir()
    assert read_run_records(tmp_path) == []


@pytest.mark.parametrize("content", [b"not json", b"[]", b"{}", b"\xff"])
def test_invalid_record_is_unknown_without_repairs(tmp_path, content):
    path = tmp_path / "runs/run-bad/record.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert read_run_records(tmp_path) == [{"run_id": "run-bad"}]
    assert path.read_bytes() == content
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "runs", "runs/run-bad", "runs/run-bad/record.json"]


def test_missing_record_is_one_unknown_run_without_repairs(tmp_path):
    directory = tmp_path / "runs/run-missing"
    directory.mkdir(parents=True)
    assert read_run_records(tmp_path) == [{"run_id": "run-missing"}]
    assert list(directory.iterdir()) == []


def test_valid_record_is_read(tmp_path):
    path = tmp_path / "runs/run-live/record.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"run_id":"run-live","host":"worker","status":"running"}')
    assert read_run_records(tmp_path) == [
        {"run_id": "run-live", "host": "worker", "status": "running"}]


def test_run_cache_reopens_only_the_record_that_changed(tmp_path, monkeypatch):
    from pathlib import Path
    from plangraph.keeper import run_records

    paths = []
    for name in ("run-a", "run-b"):
        path = tmp_path / "runs" / name / "record.json"
        path.parent.mkdir(parents=True)
        path.write_text(f'{{"run_id":"{name}","status":"running","attempts":[1,2]}}')
        paths.append(path)
    reads = []
    real = Path.read_text

    def track(path, *args, **kw):
        reads.append(path)
        return real(path, *args, **kw)

    monkeypatch.setattr(Path, "read_text", track)
    cache = run_records.RunRecordCache(fields=("run_id", "host", "status"))
    assert cache.read(tmp_path) == [{"run_id": name, "status": "running"} for name in ("run-a", "run-b")]
    paths[1].write_text('{"run_id":"run-b","status":"completed"}')
    assert cache.read(tmp_path)[1]["status"] == "completed"
    assert reads == [*paths, paths[1]]
    paths[0].unlink()
    paths[0].parent.rmdir()
    assert cache.read(tmp_path) == [{"run_id": "run-b", "status": "completed"}]
