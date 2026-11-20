"""HARNESS-RECORDED-PER-RUN: every run record carries harness, harness_version, host (and fallback)."""
import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import own_harness_legs, spawnable_legs
from plangraph.spawner import exit_outcome as hr
from plangraph.spawner.service import SpawnService

CATALOG = {leg.id: leg for leg in spawnable_legs()}
VERSIONS = {"opencode": "1.4.2", "codex": "codex-cli 0.9.1", "agy": "agy 2.0.3", "claude": "2.1.7 (Claude Code)"}


@pytest.fixture(autouse=True)
def versions(monkeypatch):
    monkeypatch.setattr(hr, "client_version", lambda name: VERSIONS.get(name, "unknown"))


@pytest.mark.parametrize("leg_id,harness", [
    ("opencode-deepseek-v4.1-flash-free", "opencode"), ("gpt-6.2-sol-medium", "codex"),
    ("agy-gemini-4-hydrogen-high", "agy"), ("claude-sonnet-5-5-high", "claude")])
def test_launch_stamps_harness_version_host(leg_id, harness):
    record = {}
    hr.stamp_harness(record, CATALOG[leg_id], host="dell")
    assert record == {"harness": harness, "harness_version": VERSIONS[harness],
                      "harness_fallback": None, "host": "dell"}


def test_fallback_run_carries_the_reason():
    (leg,) = own_harness_legs((CATALOG["cx-gpt-6.2-sol-medium"],), lambda name: False)
    record = {}
    hr.stamp_harness(record, leg, host="dell")
    assert (record["harness"], record["harness_fallback"]) == ("opencode", "client_missing")


def test_unreadable_version_is_unknown_never_guessed(monkeypatch):
    monkeypatch.undo()
    hr.client_version.cache_clear()
    monkeypatch.setattr(hr.shutil, "which", lambda name: None)
    assert hr.client_version("codex") == "unknown"
    record = {}
    hr.stamp_harness(record, CATALOG["gpt-6.2-sol-medium"], host="dell")
    assert record["harness_version"] == "unknown" and record["harness"] == "codex"


def test_native_codex_record_fields():
    assert hr.native_harness_fields("dell") == {"harness": "codex", "harness_version": "codex-cli 0.9.1",
                                        "harness_fallback": None, "host": "dell"}


def test_service_launch_stamps_the_record_before_the_client_starts(tmp_path):
    async def refuse(**kwargs):
        raise RuntimeError("stop after stamp")
    runner = SimpleNamespace(default_cwd=str(tmp_path), spawn=refuse, _tasks={}, _processes={}, jobs={})
    service = SpawnService(runner, state=tmp_path / "state")
    record = {"run_id": "run-1", "role": "implement"}
    with pytest.raises(RuntimeError):
        asyncio.run(service._launch_leg(record, CATALOG["agy-claude-sonnet-5-5-high"], prompt="x", model_or_combo="m", prompt_record=None))
    assert record["harness"] == "agy" and record["harness_version"] == VERSIONS["agy"] and record["host"]


def run(id, harness, status, ended, reason=None, role="implement", model="m"):
    return {"run_id": id, "harness": harness, "status": status, "ended": ended, "reason": reason,
            "role": role, "chosen_leg": {"model": model}}


def test_report_lists_failed_runs_of_24h_by_harness():
    now = 1_000_000.0
    runs = [run("a", "codex", "failed", now - 100), run("b", "codex", "completed", now - 100),
            run("c", "opencode", "failed", now - 3600, "ECONNREFUSED"),
            run("d", "opencode", "failed", now - 90000),            # older than 24 h
            {"run_id": "e", "status": "failed", "ended": now - 5},   # no harness recorded
            run("f", "agy", "completed", now - 10, role="review", reason="no_verdict")]
    report = hr.harness_report(runs, now=now, hours=24)
    assert report["failed"] == {"codex": ["a"], "opencode": ["c"], "unknown": ["e"]}
    assert report["no_verdict"] == {"agy": ["f"]}
    assert report["runs"] == {"codex": 2, "opencode": 1, "unknown": 1, "agy": 1}


def review(id, ended, leg, reason, status="failed"):
    return {"run_id": id, "role": "review", "ended": ended, "reason": reason, "status": status,
            "attempts": [{"status": "failed", "leg": {"id": leg}}]}


def test_three_silent_reviews_in_a_row_put_a_leg_out_and_a_verdict_resets(tmp_path):
    now = 1_000_000.0
    for i in range(3):
        hr.count_review_outcome(tmp_path, review(str(i), now - 7200 + i, "gpt-6.2-sol-medium", "no_verdict"))
    hr.count_review_outcome(tmp_path, review("m1", now - 100, "mimo-v2.6-pro", "no_verdict"))
    hr.count_review_outcome(tmp_path, review("m2", now - 90, "mimo-v2.6-pro", None, status="completed"))
    for i in range(2):
        hr.count_review_outcome(tmp_path, review(f"a{i}", now - 80 + i, "agy-gemini-4-argon-low", "no_verdict"))
    assert hr.silent_review_legs(tmp_path, now=now) == {"gpt-6.2-sol-medium"}   # two hours later: still out
    assert hr.silent_review_legs(tmp_path, now=now + 86400) == set()   # retried after the ttl
    hr.count_review_outcome(tmp_path, review("ok", now, "gpt-6.2-sol-medium", None, status="completed"))
    assert hr.silent_review_legs(tmp_path, now=now) == set()


def test_a_run_counts_once_and_other_endings_do_not_count(tmp_path):
    record = review("x", 100.0, "gpt-6.2-sol-medium", "no_verdict")
    for _ in range(5):
        hr.count_review_outcome(tmp_path, record)
    assert record["no_verdict_counted"] is True
    hr.count_review_outcome(tmp_path, review("q", 101.0, "gpt-6.2-sol-medium", "harness_limit"))
    hr.count_review_outcome(tmp_path, dict(review("w", 102.0, "gpt-6.2-sol-medium", "no_verdict"), role="implement"))
    assert json.loads((tmp_path / "review-silent.json").read_text())["gpt-6.2-sol-medium"]["count"] == 1


def silent_service(tmp_path):
    service = SpawnService(SimpleNamespace(default_cwd=str(tmp_path), jobs={}, _tasks={}, _processes={}),
                           state=tmp_path / "state")
    now = time.time()
    for i in range(3):   # a persisted review record counts through the service save path
        record = review(f"run-{i}x", now - 7200 + i, "gpt-6.2-sol-medium", "no_verdict")
        record.update(started=now - 7300, host=hr._host_name())
        service._save(record)
    return service


def test_route_drops_a_silent_review_leg_even_after_the_cooldown_window(tmp_path):
    from pathlib import Path
    service = silent_service(tmp_path)
    card = {"estimate_tokens": 1000, "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
    plan = service.route(card, "review", ("claude",))
    assert "gpt-6.2-sol-medium" not in [leg.id for leg in plan.legs]
    assert any(s["leg"] == "gpt-6.2-sol-medium" and s["reason"] == "no_verdict_leg" for s in plan.skipped)
    assert [leg.id for leg in plan.review_seats] == ["mimo-v2.6-pro", "opencode-deepseek-v4.1-flash-free"]
    assert [row["leg"] for row in plan.seat_coverage] == [leg.id for leg in plan.review_seats]


def test_cli_harness_report_lists_failed_runs_by_harness(tmp_path, capsys):
    from plangraph.spawner.cli import main
    from plangraph.spawner.private_io import atomic_write_private
    service = SpawnService(SimpleNamespace(default_cwd=str(tmp_path), jobs={}, _tasks={}, _processes={}),
                           state=tmp_path / "state")
    record = run("run-1a", "codex", "failed", time.time() - 60)
    path = service._path("run-1a")
    path.parent.mkdir(parents=True)
    atomic_write_private(path, json.dumps(dict(record, started=time.time() - 120)))
    assert main(["--state", str(tmp_path / "state"), "harness-report"]) == 0
    assert json.loads(capsys.readouterr().out)["failed"] == {"codex": ["run-1a"]}


def test_stamp_keeps_the_recorded_host_and_names_the_cooldown_host_otherwise(monkeypatch):
    record = {"host": "placed-host"}
    hr.stamp_harness(record, CATALOG["agy-claude-sonnet-5-5-high"])
    assert record["host"] == "placed-host"
    monkeypatch.setenv("PLANGRAPH_HOST", "cooldown-host")
    bare = {}
    hr.stamp_harness(bare, CATALOG["agy-claude-sonnet-5-5-high"])
    assert bare["host"] == "cooldown-host"
