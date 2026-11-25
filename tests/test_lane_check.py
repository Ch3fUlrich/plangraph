"""The Python entry to the duplicate-assignment guard and the native dispatch boundary."""
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import plangraph.fleet_snapshot as lane_check
from plangraph.routing_errors import SpawnRefused

REFUSAL = "lane_conflict: p:X is held by native run lane-one (run-a); pass a lane_override reason"


def fake_binary(tmp_path, *, code=0, stderr="", native_tools="{}", real=None):
    """A stand-in pg-spawnd: logs the lane-check input, answers native-tools; with ``real`` the
    lane-check verb runs the built guard instead of a canned answer."""
    script = tmp_path / "pg-spawnd"
    script.write_text(f"""#!{sys.executable}
import subprocess, sys
if sys.argv[1] == "lane-check":
    data = sys.stdin.read()
    open({str(tmp_path / 'lane-check.json')!r}, "w").write(data)
    if {real!r}:
        done = subprocess.run([{real!r}, "lane-check"], input=data, text=True, capture_output=True)
        sys.stderr.write(done.stderr)
        sys.exit(done.returncode)
    sys.stderr.write({stderr!r})
    sys.exit({code})
if sys.argv[1] == "native-tools":
    print({native_tools!r})
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_a_refusal_of_the_guard_becomes_an_exception(tmp_path):
    binary = fake_binary(tmp_path, code=2, stderr=REFUSAL)
    with pytest.raises(lane_check.LaneRefused, match="held by native run lane-one"):
        lane_check.lane_check({"prompt": "Item key: p:X"}, binary=binary)
    sent = json.loads((tmp_path / "lane-check.json").read_text())
    assert sent["prompt"] == "Item key: p:X" and sent["state"]


def test_a_guard_that_cannot_answer_does_not_refuse(tmp_path):
    for code, stderr in ((0, ""), (2, "unknown or duplicate flag"), (1, "")):
        lane_check.lane_check({}, binary=fake_binary(tmp_path, code=code, stderr=stderr))
    lane_check.lane_check({}, binary=str(tmp_path / "missing"))


def test_an_override_without_a_reason_is_a_refusal(tmp_path):
    binary = fake_binary(tmp_path, code=2, stderr="lane_override requires a non-empty reason")
    with pytest.raises(lane_check.LaneRefused):
        lane_check.lane_check({"reason": " "}, binary=binary)


def built_guard():
    """The built pg-spawnd (``cargo build -p pg-spawnd``), the real guard these tests drive."""
    root = Path(__file__).resolve().parents[1] / "rust" / "target"
    for candidate in (os.environ.get("PG_SPAWND_BIN"), root / "debug" / "pg-spawnd",
                      root / "release" / "pg-spawnd"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    pytest.skip("pg-spawnd is not built: cargo build -p pg-spawnd")


def native_dispatch(monkeypatch, tmp_path, card_extra, prompt="Item key: p:X\nNAME: lane-two\n"):
    """Run native_spawn to the lane guard with the built guard and an empty roster but one live run."""
    from plangraph import fleet_snapshot
    from plangraph.spawner import host_ready, overflow
    from plangraph.spawner.service import SpawnService

    binary = fake_binary(tmp_path, native_tools='{"sandbox_ok": true}', real=built_guard())
    monkeypatch.setattr(fleet_snapshot, "_projection_binary", lambda: binary)
    monkeypatch.setenv("ROUTING_STATE_DIR", str(tmp_path / "routing"))
    monkeypatch.delenv("PG_URL", raising=False)

    def no_lease():
        raise RuntimeError("stop after the guard")
    monkeypatch.setattr(host_ready, "cargo_lease", no_lease)
    state = tmp_path / "state"
    run = state / "runs" / "run-held"
    run.mkdir(parents=True, exist_ok=True)
    (run / "record.json").write_text(json.dumps(
        {"run_id": "run-held", "run_kind": "native-codex", "status": "running"}))
    (run / "native-request.json").write_text(json.dumps(
        {"prompt": "Item key: p:X\nNAME: lane-one\n", "card": {}}))
    service = SpawnService(state=str(state))
    card = {"repo": str(tmp_path), "run_kind": "native-codex", "item": "p:X", **card_extra}
    return state, asyncio.run(overflow.native_spawn(service, card, {"prompt": prompt}))


def test_native_dispatch_with_the_built_guard_refuses_blank_and_admits_a_recorded_override(
        monkeypatch, tmp_path):
    # `item` is the card alias the run record also accepts: the guard must see it.
    with pytest.raises(SpawnRefused, match=r"p:X is held by native run lane-one \(run-held\)"):
        native_dispatch(monkeypatch, tmp_path, {})
    with pytest.raises(SpawnRefused, match="non-empty reason"):
        native_dispatch(monkeypatch, tmp_path, {"lane_override": "  "})
    state, record = native_dispatch(monkeypatch, tmp_path, {"lane_override": "lane-one is wedged"})
    assert record["status"] == "failed"      # past the guard: the stub lease stopped the launch
    log = [json.loads(line) for line in (state / "lane-overrides.jsonl").read_text().splitlines()]
    assert log[-1]["reason"] == "lane-one is wedged" and log[-1]["context"] == "dispatch"
    assert log[-1]["held"][0]["key"] == "p:X"


def test_native_dispatch_publishes_no_run_when_the_override_cannot_be_recorded(
        monkeypatch, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "lane-overrides.jsonl").mkdir()      # unwritable as a log
    with pytest.raises(SpawnRefused, match="cannot record the lane override"):
        native_dispatch(monkeypatch, tmp_path, {"lane_override": "lane-one is wedged"})
    assert sorted(p.name for p in (tmp_path / "state" / "runs").iterdir()) == ["run-held"]


def test_strict_dispatch_refuses_when_the_guard_gives_no_decision(tmp_path):
    for binary in (fake_binary(tmp_path, code=2, stderr="cannot record the lane override: denied"),
                   str(tmp_path / "missing")):
        with pytest.raises(lane_check.LaneRefused, match="gave no decision"):
            lane_check.lane_check({}, binary=binary, strict=True)


def test_native_dispatch_refuses_a_held_item_before_any_record_exists(monkeypatch, tmp_path):
    from plangraph import fleet_snapshot
    from plangraph.spawner import overflow
    from plangraph.spawner.service import SpawnService

    binary = fake_binary(tmp_path, code=2, stderr=REFUSAL, native_tools='{"sandbox_ok": true}')
    monkeypatch.setattr(fleet_snapshot, "_projection_binary", lambda: binary)
    state = tmp_path / "state"
    service = SpawnService(state=str(state))
    card = {"repo": str(tmp_path), "run_kind": "native-codex", "item_key": "p:X",
            "lane_override": "wedged"}
    options = {"prompt": "Item key: p:X\nNAME: lane-two\n"}
    with pytest.raises(SpawnRefused, match="held by native run lane-one"):
        asyncio.run(overflow.native_spawn(service, card, options))
    assert not (state / "runs").exists() or not list((state / "runs").iterdir())
    sent = json.loads((tmp_path / "lane-check.json").read_text())
    assert sent["reserve"]["run_id"].startswith("run-")
    assert sent["reason"] == "wedged" and sent["context"] == "dispatch"
    assert sent["card"]["item_key"] == "p:X" and sent["state"] == str(state)


def test_the_writer_brief_generator_refuses_a_held_item(monkeypatch, tmp_path):
    from plangraph import fleet_snapshot
    from plangraph.review.prompt import render_brief

    binary = fake_binary(tmp_path, code=2, stderr=REFUSAL)
    monkeypatch.setattr(fleet_snapshot, "_projection_binary", lambda: binary)
    with pytest.raises(ValueError, match="Writer brief refused: lane_conflict"):
        render_brief("writer", {"item_key": "p:X", "lane_override": None}, rules="Item key: p:X\n")
    sent = json.loads((tmp_path / "lane-check.json").read_text())
    assert sent["keys"] == ["p:X"] and sent["context"] == "brief-generator"
    # A review brief is not an assignment: no guard call.
    (tmp_path / "lane-check.json").unlink()
    render_brief("review", {"item_key": "p:X"})
    assert not (tmp_path / "lane-check.json").exists()


def test_the_roster_fixture_is_the_body_merge_batch_land_generates(monkeypatch):
    """The Rust roster reads this body: it must stay what render_body really writes."""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    monkeypatch.syspath_prepend(tools)
    import merge_batch_land as land

    monkeypatch.setattr(land, "go_key_block", lambda st: (
        "GO-KEYS [demo:ITEM-9,demo:ITEM-10,demo:KEYS-ONLY]; GO-DEFERRED [demo:DEFERRED]"))
    state = {"name": "train-9", "base": "main", "skipped": [],
             "merged": [{"pr": 11, "items": ["demo:ITEM-9"], "head": "a" * 40, "merge": "b" * 40,
                         "resolved": False, "files": []},
                        {"pr": 12, "items": ["demo:ITEM-10"], "head": "c" * 40, "merge": "d" * 40,
                         "resolved": False, "files": []}],
             "prs": {"11": {"title": "first"}, "12": {"title": "second"}}}
    fixture = Path(__file__).resolve().parents[1] / "rust/worker-core/tests/fixtures/batch-pr-body.md"
    assert land.render_body(state) == fixture.read_text()
