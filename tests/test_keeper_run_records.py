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


def test_valid_record_is_read(tmp_path):
    path = tmp_path / "runs/run-live/record.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"run_id":"run-live","host":"worker","status":"running"}')
    assert read_run_records(tmp_path) == [
        {"run_id": "run-live", "host": "worker", "status": "running"}]
