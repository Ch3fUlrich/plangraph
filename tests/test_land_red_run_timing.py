"""A red Actions run is imported into the landing timeline before the refusal, and a listed item is found."""
import pytest


@pytest.fixture
def land(tools_importable):
    import merge_batch_land
    return merge_batch_land


def red_run(head, **over):
    run = {"id": 7, "head_sha": head, "head_branch": "batch/x", "event": "pull_request",
           "status": "completed", "conclusion": "failure", "run_attempt": 1}
    run.update(over)
    return run


def test_a_red_run_is_imported_before_the_refusal(land, monkeypatch):
    head = "a" * 40
    imported = []
    monkeypatch.setattr(land, "gh_json", lambda *args: {"workflow_runs": [red_run(head)]})
    monkeypatch.setattr(land, "run_timing_import", lambda *args: imported.append(args))
    with pytest.raises(land.MergeBatchError, match="not green"):
        land.require_full_run("owner/proj", head, "batch/x", "train-1")
    assert [(a[1], a[2], a[3]["id"], a[4]) for a in imported] == [("train-1", head, 7, 1)]


def test_a_run_on_another_head_or_still_running_is_not_imported(land, monkeypatch):
    head = "a" * 40
    imported = []
    monkeypatch.setattr(land, "run_timing_import", lambda *args: imported.append(args))
    for run in (red_run("b" * 40), red_run(head, status="in_progress", conclusion=None)):
        monkeypatch.setattr(land, "gh_json", lambda *args, run=run: {"workflow_runs": [run]})
        with pytest.raises(land.MergeBatchError):
            land.require_full_run("owner/proj", head, "batch/x", "train-1")
    assert imported == []


def test_a_push_gate_stage_is_delivered_with_its_host_result_and_sub_stage(tools_importable, monkeypatch, tmp_path):
    import json
    import stat

    import pgtest
    stub = tmp_path / "plangraph"
    log = tmp_path / "body.json"
    stub.write_text(f"#!/bin/sh\ncat > {log}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("PG_LANDING_REPO", "owner/proj")
    monkeypatch.setenv("PG_LANDING_BATCH", "batch-1")
    monkeypatch.setenv("PG_LANDING_STAGE_HEAD", "a" * 40)
    pgtest.deliver_landing_stage("fast_checks", 1000, 4000, "failed", "rust")
    body = json.loads(log.read_text())
    assert (body["stage"], body["detail"], body["result"], body["attempt"]) == ("fast_checks", "rust", "failed", 1000)
    assert (body["started_at_ms"], body["ended_at_ms"], body["batch_key"]) == (1000, 4000, "batch-1")
    assert body["host"]
    log.unlink()
    monkeypatch.delenv("PG_LANDING_BATCH")
    pgtest.deliver_landing_stage("fast_checks", 1000, 4000, "ok")
    assert not log.exists()  # no lander, no stage
