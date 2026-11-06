import json
import os

import pytest

from plangraph.keeper.cli import build_parser
from plangraph.keeper.cli_local import evidence_ref
from plangraph.keeper.evidence import RunRecordVerifier, default_context
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter

ITEM = {"key": "project:ITEM", "id": "item-1"}
REF = {"run_id": "run-1", "host": "host-a", "writer": "leg-1"}


@pytest.fixture
def mirror(tmp_path):
    path = tmp_path / "host-a" / "run-1" / "record.json"
    path.parent.mkdir(parents=True)
    record = dict(run_id="run-1", host="host-a", item_key=ITEM["key"],
                  status="completed", exit_code=0, chosen_leg={"id": "leg-1"},
                  writer_family="family-1", served_family="unwitnessed")
    path.write_text(json.dumps(record))
    for component in (path.parent.parent, path.parent, path):
        component.chmod(0o700 if component.is_dir() else 0o600)
    return tmp_path, path, record


def check(mirror, ref=None, adapter=None):
    root, _, _ = mirror
    ctx = default_context({"spawner_records_dir": root})
    if adapter is not None:
        ctx.spawner_records = adapter
    return RunRecordVerifier().verify(ITEM, REF if ref is None else ref, ctx)


def test_other_host_passes(mirror):
    result = check(mirror)
    assert result.ok
    assert result.detail == "run run-1 on host-a for project:ITEM rc=0 leg=leg-1 family=family-1"


@pytest.mark.parametrize("field,value,reason", [
    ("item_key", "project:OTHER", "item_key"),
    ("item_key", None, "item_key"),
    ("item_key", "DELETE", "item_key"),
    ("run_id", "run-2", "run_id"), ("host", "host-b", "host"),
    ("status", "failed", "status"), ("exit_code", 1, "exit_code"),
    ("exit_code", True, "exit_code"), ("writer_family", "", "writer_family"),
    ("served_family", "other", "served_family"),
    ("chosen_leg", None, "leg"), ("writer_family", None, "writer_family"),
    ("served_family", None, "served_family"),
])
def test_record_binding_refused(mirror, field, value, reason):
    _, path, record = mirror
    record[field] = value
    if value == "DELETE":
        del record[field]
    record["stdout_tail"] = "SECRET"
    path.write_text(json.dumps(record))
    result = check(mirror)
    assert not result.ok and reason in result.detail
    assert "SECRET" not in result.detail


@pytest.mark.parametrize("writer", [None, "other", ""])
def test_writer_refused(mirror, writer):
    ref = dict(REF)
    if writer is None:
        del ref["writer"]
    else:
        ref["writer"] = writer
    result = check(mirror, ref)
    assert not result.ok and "writer" in result.detail


def test_unset(mirror):
    result = check(mirror, adapter=SpawnerRecordAdapter(None))
    assert not result.ok and "not configured" in result.detail


def test_missing(mirror):
    mirror[1].unlink()
    assert "missing" in check(mirror).detail


@pytest.mark.parametrize("host", ["..", "host-a/other", "host-a\n"])
def test_host_shape(mirror, host):
    result = check(mirror, dict(REF, host=host))
    assert not result.ok and "host" in result.detail


@pytest.mark.parametrize("component", ["host", "run", "file", "outside"])
def test_symlinks(mirror, component):
    root, path, _ = mirror
    source = {"host": root / "host-a", "run": path.parent,
              "file": path, "outside": path}[component]
    target = root.parent / "outside" if component == "outside" else root / "target"
    source.rename(target)
    source.symlink_to(target)
    assert not check(mirror).ok


@pytest.mark.parametrize("payload,reason", [("[]", "object"), ("{", "malformed")])
def test_json_refused(mirror, payload, reason):
    mirror[1].write_text(payload)
    result = check(mirror)
    assert not result.ok and reason in result.detail


def test_oversize_before_read(mirror, monkeypatch):
    with mirror[1].open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    monkeypatch.setattr(os, "fdopen", lambda *a: pytest.fail("oversize file read"))
    result = check(mirror)
    assert not result.ok and "size" in result.detail


@pytest.mark.parametrize("component,mode", [("file", 0o620), ("dir", 0o707)])
def test_writable_refused(mirror, component, mode):
    path = mirror[1] if component == "file" else mirror[1].parent
    path.chmod(mode)
    result = check(mirror)
    assert not result.ok and "writable" in result.detail


def test_owner_refused(mirror):
    result = check(mirror, adapter=SpawnerRecordAdapter(mirror[0], uid=os.geteuid() + 1))
    assert not result.ok and "owner" in result.detail


def test_nonregular(mirror):
    mirror[1].unlink()
    mirror[1].mkdir()
    assert not check(mirror).ok


def test_symlink_swap_at_open(mirror, monkeypatch):
    root, path, _ = mirror
    original = os.open

    def raced(name, flags, **kwargs):
        if name == "record.json":
            target = root / "replacement"
            path.rename(target)
            path.symlink_to(target)
        return original(name, flags, **kwargs)

    monkeypatch.setattr(os, "open", raced)
    assert not check(mirror).ok


def test_legacy_does_not_read_mirror(mirror, monkeypatch):
    root, path, _ = mirror
    (path.parent / "job.json").write_text(json.dumps({"request": {"item_key": ITEM["key"]}}))
    (path.parent / "exit.json").write_text('{"rc": 0}')
    monkeypatch.setattr(SpawnerRecordAdapter, "record", lambda *a: pytest.fail("mirror read"))
    ctx = default_context({"runs_dir": root / "host-a"})
    assert RunRecordVerifier().verify(ITEM, {"run_id": "run-1"}, ctx).ok


@pytest.mark.parametrize("flags,expected", [([], {"run_id": "run-1"}),
    (["--host", "host-a", "--writer", "leg-1"], REF)])
def test_cli(flags, expected):
    args = build_parser().parse_args(["evidence", ITEM["key"], "--kind", "run-record",
                                      "--run", "run-1", *flags])
    assert evidence_ref(args) == expected
