"""The Python entry to the duplicate-assignment guard and the native dispatch boundary."""
import asyncio
import json
import stat
import sys

import pytest

from plangraph.spawner import item_key as lane_check
from plangraph.routing_errors import SpawnRefused

REFUSAL = "lane_conflict: p:X is held by native run lane-one (run-a); pass a lane_override reason"


def fake_binary(tmp_path, *, code=0, stderr="", native_tools="{}"):
    """A stand-in pg-spawnd: logs the lane-check input, answers native-tools."""
    script = tmp_path / "pg-spawnd"
    script.write_text(f"""#!{sys.executable}
import sys
if sys.argv[1] == "lane-check":
    open({str(tmp_path / 'lane-check.json')!r}, "w").write(sys.stdin.read())
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
