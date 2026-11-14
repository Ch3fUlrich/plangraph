"""D-3649: a stalled, timed-out or cancelled run keeps the events annotate() put on its record (hermetic)."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from plangraph.spawner.run_watchdog import terminal_record
from plangraph.spawner.runner import SpawnerRunner, spawn_service

from test_w4_regressions import _gateway_url, fake_host_binaries  # noqa: F401  (autouse: hermetic gateway URL, fake harness binaries)

ANNOTATED = [{"event": "big_file_unsliced", "detail": "big_file_unsliced: big.py 500 lines; name one function per slice in card.context"},
             {"event": "context_range_past_eof", "detail": "context_range_past_eof: a.py:9-20 (file has 10 lines; clamped)"}]


def job(status, *events):
    return SimpleNamespace(status=status, exit_code=6, events=list(events))


def test_terminal_record_merges_instead_of_overwriting():
    watchdog = {"event": "watchdog_stall", "rule": "no-activity"}
    for status in ("stalled", "timeout"):
        record = {"events": list(ANNOTATED)}
        assert terminal_record(record, job(status, watchdog)) is True
        assert record["events"] == ANNOTATED + [watchdog]
        assert record["watchdog"] == watchdog and record["status"] == status


def test_terminal_record_without_earlier_events_and_for_other_statuses():
    watchdog = {"event": "watchdog_max"}
    record = {}
    assert terminal_record(record, job("timeout", watchdog)) is True and record["events"] == [watchdog]
    record = {"events": list(ANNOTATED)}
    assert terminal_record(record, job("completed")) is False and record["events"] == ANNOTATED


@pytest.mark.parametrize('phase,budget', [('watchdog', '5'), ('admission', '0.000001')])
def test_dispatch_timeout_keeps_the_big_file_event(tmp_path, monkeypatch, phase, budget):
    """The dispatch loop's own terminal branch (runner.py), driven end to end by the run timeout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "big.py").write_text("".join(f"x{i}\n" for i in range(500)))
    subprocess.run(["git", "-C", str(repo), "add", "big.py"], check=True)
    monkeypatch.setenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", budget)
    monkeypatch.setenv("PLANGRAPH_SPAWN_WATCHDOG_INTERVAL", "0.05")

    launched = []
    async def factory(*args, **kwargs):
        launched.append(True)
        return await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)

    async def run():
        service = spawn_service(SpawnerRunner(process_factory=factory, default_cwd=str(repo)), tmp_path / "state")
        return await service.spawn(card={"estimate_tokens": 1000}, prompt="edit big.py", wait_seconds=None)

    record = asyncio.run(run())
    assert record["status"] == "timeout", record["status"]
    names = [e["event"] for e in record["events"]]
    timeout_event = next((n for n in names if n.startswith("watchdog_")), None) if phase == "watchdog" else "run_timeout"
    assert bool(launched) == (phase == "watchdog")
    if phase == "admission": assert not any(n.startswith("watchdog_") for n in names)
    assert names.count("big_file_unsliced") == 1 and timeout_event is not None
    assert names.index("big_file_unsliced") < names.index(timeout_event)
    assert record["warnings"] and record["warnings"][0].startswith("big_file_unsliced: big.py 500 lines")
