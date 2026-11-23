"""Configured spawner adapters survive the Store's verifier context rebuild."""
import json
from pathlib import Path

import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper.config import Config, verifier_context
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.keeper.store import Store

REF = {"run_id": "run-1", "host": "host-other", "writer": "leg-1"}


@pytest.fixture
def keeper(tmp_path):
    mirror = tmp_path / "mirror"
    record_path = mirror / REF["host"] / REF["run_id"] / "record.json"
    record_path.parent.mkdir(parents=True)
    for directory in (mirror, record_path.parent.parent, record_path.parent):
        directory.chmod(0o700)
    cfg = Config({"spawner_records_dir": str(mirror),
                  "runs_dir": str(tmp_path / "runs")}, tmp_path)
    world = World(tmp_path, backend="sqlite")
    world.store = Store(world.conn, clock=world.clock, ctx=verifier_context(cfg))
    try:
        (key,) = seed(world, [("ITEM", [])], evidence_kinds=("run-record",))
        epoch = to_claimed(world, key)
        record_path.write_text(json.dumps(dict(
            REF, item_key=key, status="completed", exit_code=0,
            chosen_leg={"id": "leg-1"}, writer_family="family-1",
            served_family="unwitnessed")))
        record_path.chmod(0o600)

        def evidence(ref):
            return world.store.add_evidence(
                world.p("l1main"), key, epoch, "run-record", ref)

        yield world, cfg, record_path, key, evidence
    finally:
        world.close()


def test_other_host_verifies_through_store(keeper):
    _, _, _, key, evidence = keeper
    result = evidence(REF)
    assert result["verified"] is True
    assert result["detail"] == (
        f"run run-1 on host-other for {key} rc=0 leg=leg-1 family=family-1")


@pytest.mark.parametrize("case,detail", [
    ("completed", "rc=0"), ("running", "status is not completed"),
    ("symlink", "unreadable or symlinked"), ("symlinked-directory", "unreadable or symlinked"),
    ("writable-record", "writable by group or other"),
    ("wrong-ref-host", "record is missing"), ("wrong-record-host", "host mismatch"),
])
def test_local_review_through_store(keeper, monkeypatch, case, detail):
    world, cfg, remote, key, evidence = keeper
    monkeypatch.setenv("PLANGRAPH_HOST", "local-review-host")
    world.store.ctx = verifier_context(cfg)
    runs = Path(cfg.runs_dir)
    path = runs / "run-local" / "record.json"
    path.parent.mkdir(parents=True)
    runs.chmod(0o775)
    record = dict(json.loads(remote.read_text()), run_id="run-local", role="review",
                  host="foreign-host" if case == "wrong-record-host" else "local-review-host",
                  served_family="anthropic", writer_family="anthropic",
                  status="running" if case == "running" else "completed")
    path.write_text(json.dumps(record))
    if case == "writable-record":
        path.chmod(0o660)
    if case == "symlink":
        target = runs / "target.json"
        path.rename(target)
        path.symlink_to(target)
    if case == "symlinked-directory":
        target = runs / "target"
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    ref = {"run_id": "run-local", "host": record["host"], "writer": "leg-1"}
    if case == "wrong-record-host":
        ref["host"] = "local-review-host"
    if case == "wrong-ref-host":
        ref["host"] = "foreign-host"
    result = evidence(ref)
    assert detail in result["detail"]
    assert result["verified"] is (case == "completed")
    if result["verified"]:
        assert f"for {key}" in result["detail"]


def test_local_witness_discovery_without_mirror(keeper, monkeypatch):
    _, cfg, _, key, _ = keeper
    monkeypatch.setenv("PLANGRAPH_HOST", "local-review-host")
    cfg.spawner_records_dir = None
    head = "a" * 40
    run_id = "run-local"
    runs = Path(cfg.runs_dir)
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(runs.parent))
    cfg.runs_dir = None
    local = runs / run_id / "record.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({
        "host": "local-review-host", "run_id": run_id, "item_key": key, "role": "review",
        "status": "completed", "exit_code": 0, "served_model": "gemini-test",
        "finish_reason": "stop", "attempts": [{}], "completion": {"head_sha": head},
        "stdout_tail": "TEST_GAMING: no\nVERDICT: PASS\n",
    }))
    adapter = verifier_context(cfg).spawner_records
    assert adapter.review_seats({"key": key}, head, f"Review-Runs: {run_id}") == [
        {"model": "gemini-test", "verdict": "pass", "run_id": run_id, "head": head}]


def test_forged_writer_refused_through_store(keeper):
    result = keeper[-1](dict(REF, writer="forged-leg"))
    assert result["verified"] is False
    assert result["detail"] == "record writer leg mismatch"


def test_missing_record_refused_through_store(keeper):
    keeper[2].unlink()
    result = keeper[-1](REF)
    assert result["verified"] is False
    assert result["detail"] == "spawner record is missing"


def test_unset_mirror_refused_through_store(keeper):
    world, cfg, _, _, evidence = keeper
    cfg.spawner_records_dir = None
    world.store.ctx = verifier_context(cfg)
    result = evidence(REF)
    assert result["verified"] is False
    assert result["detail"] == "spawner_records_dir is not configured"


def test_hostless_ref_uses_local_run_through_store(keeper, monkeypatch):
    _, cfg, _, key, evidence = keeper
    local = Path(cfg.runs_dir) / REF["run_id"]
    local.mkdir(parents=True)
    (local / "job.json").write_text(json.dumps({"request": {"item_key": key}}))
    (local / "exit.json").write_text('{"rc": 0}')
    monkeypatch.setattr(SpawnerRecordAdapter, "record",
                        lambda *args: pytest.fail("hostless ref read the mirror"))
    assert evidence({"run_id": REF["run_id"]})["verified"] is True
