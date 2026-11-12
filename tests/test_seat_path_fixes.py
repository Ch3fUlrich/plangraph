"""Hermetic regressions for SEAT-PATH-FIXES (D-3676)."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.review.verdict import parse_verdict
from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
from plangraph.spawner.run_watchdog import Watchdog, thresholds, terminal_record


def test_inline_review_without_diff_ref(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a document review must not invoke git")
    monkeypatch.setattr("plangraph.spawner.inline_diff.subprocess.run", forbidden)
    assert inline_diff_prompt("Review this design", None, max_chars=1, allow_document=True) == "Review this design"
    with pytest.raises(InlineDiffError, match="no diff_ref"):
        inline_diff_prompt("Review this design", None, max_chars=1)
    with pytest.raises(InlineDiffError):
        inline_diff_prompt("Review", {}, max_chars=1)


def test_mimo_flash_is_inline_only():
    assert next(l for l in DEFAULT_LEGS if l.id == "mimo-v2.6-flash").tool_mode == "inline-only"


def test_verdict_commentary_and_parser_parity(tools_importable):
    # conftest's scoped tools path supplies seat_record; it never skips this check.
    spec = importlib.util.spec_from_file_location("seat_verdict", Path(__file__).parents[1] / "tools/seat_verdict.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    # Historical run stdout was unavailable; these are synthetic regression cases.
    for line, expected in [
        ("VERDICT: REQUEST_CHANGES — HIGH: broken contract", "fix"),
        ("VERDICT: ACCEPT — no blocking findings", "pass"),
        ("VERDICT: PASS: no blockers", "pass"),
        ("VERDICT: PASS? commentary follows", None),
        ("VERDICT: pass | fix", None),
        ("VERDICT: <pass|fix>", None),
        ("VERDICT: request_changes / accept", None),
        ("VERDICT: unknown commentary", None),
    ]:
        text = "VERDICT: halt\n" + line
        assert parse_verdict(text) == expected
        assert tool.parse_verdict(text)[0] == {"fix": False, "pass": True, None: None}[expected]
    # Parity per rule: halt, low-label downgrade, and multi-VERDICT ordering
    # (the last VERDICT line decides even when it is no decision at all).
    for text, expected in [
        ("VERDICT: halt", "halt"),
        ("VERDICT: halt — low: nits only", "halt"),
        ("VERDICT: fix — low: nit", "pass"),
        ("VERDICT: changes: LOW: docs only", "pass"),
        ("VERDICT: fix — low: nit, HIGH: broken contract", "fix"),
        ("VERDICT: pass\nVERDICT: fix — high: broken", "fix"),
        ("VERDICT: pass\nVERDICT: pass | fix", None),
        ("VERDICT: fix\nVERDICT: unknown commentary", None),
        # r3: a template echo (fenced or quoted) never decides and never flips a
        # real verdict; the last line rule sees only the remaining lines.
        ("VERDICT: fix\nVERDICT: <pass|fix>", None),
        ("VERDICT: fix\n```\nVERDICT: pass\n```", "fix"),
        ("VERDICT: fix\n> VERDICT: pass", "fix"),
    ]:
        assert parse_verdict(text) == expected
        assert tool.parse_verdict(text)[0] == {"fix": False, "pass": True, "halt": False, None: None}[expected]


def test_review_default_wall_cap(monkeypatch, tmp_path):
    monkeypatch.delenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", raising=False)
    limits = thresholds(role="review")
    assert limits["max_s"] == 1500 and limits["hard_max"]
    watchdog = Watchdog(limits, start=0, edit=None, writer=False)
    assert watchdog.decide(1499, None, None, None) is None
    event = watchdog.decide(1500, None, None, None)
    assert event["rule"] == "max"
    assert thresholds()["max_s"] == thresholds(role="orchestrate")["max_s"] == 5400
    assert thresholds({"max_min": 2}, role="review")["max_s"] == 120
    monkeypatch.setenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", "7")
    assert thresholds(role="review")["max_s"] == 7
    assert thresholds({"max_min": 2}, role="review")["max_s"] == 120
    monkeypatch.setenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", "0.3")

    # r3: the real prepare_run/collect path ends a capped run (no fabricated
    # exit 124); only a review records 'timed_out', any other role keeps 'timeout'.
    async def capped(role):
        from plangraph.spawner.runner import SpawnerRunner, JobInfo
        from plangraph.spawner.run_watchdog import collect, edit_signature, prepare_run
        record = {"run_id": "run-x", "role": role}
        service = SimpleNamespace(_path=lambda run_id: tmp_path / run_id / "r.json")
        limits, live_log = prepare_run(service, record, {"stall_min": 10, "first_edit_min": 20})
        runner = SpawnerRunner(default_cwd=str(tmp_path))
        job = JobInfo("j", "running", "", "m", role=role, cwd=str(tmp_path))
        job._watchdog_limits = limits
        job._watchdog_baseline = edit_signature(tmp_path)
        job._live_log = Path(live_log)
        runner.jobs["j"] = job
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)", cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        from plangraph.spawner.proc_kill import root_identity
        runner._owned = {"j": dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        await collect(runner, "j", proc)
        assert job.status == "timeout" and job.exit_code == 124
        assert terminal_record(record, job)
        return record
    assert asyncio.run(capped("implement"))["status"] == "timeout"
    assert asyncio.run(capped("review"))["status"] == "timed_out"


def test_empty_run_timeout_keeps_default_limits(monkeypatch):
    monkeypatch.setenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", "")
    for role in ("review", "implement", "orchestrate", None):
        limits = thresholds(role=role)
        assert limits["max_s"] == 5400
        assert limits["hard_max"] is (role == "review")
    monkeypatch.delenv("PLANGRAPH_SPAWN_RUN_TIMEOUT")
    limits = thresholds(role="review")
    assert limits["max_s"] == 1500
    assert limits["hard_max"] is True


def test_timed_out_is_read_as_timeout():
    # r3: the review wall-cap spelling maps back to 'timeout' in the reason
    # reader; a run that ended on the quota leg keeps the timeout reason.
    from plangraph.spawner.exit_outcome import terminal_reason
    record = {"status": "timed_out", "reason": "harness_limit",
              "attempts": [{"status": "skipped"},
                           {"status": "timed_out", "skip_reason": "harness_limit"}]}
    terminal_reason(record)
    assert record["reason"] == "timeout"


def test_inline_only_review_leg_requires_diff_ref(monkeypatch, tmp_path):
    # r3: a routed inline-only review leg refuses without diff_ref; only an
    # explicit --leg document review proceeds with the prompt unchanged.
    from plangraph.spawner.seat_spawn import SeatSpawnMixin
    monkeypatch.setattr("plangraph.spawner.context_pack.pack_prompt",
                        lambda prompt, card, cwd, pack: prompt)

    class _Seat(SeatSpawnMixin):
        def __init__(self):
            self.runner = SimpleNamespace(default_cwd=str(tmp_path))
    leg = SimpleNamespace(tool_mode="inline-only", max_request_tokens=100)
    seat = _Seat()
    with pytest.raises(InlineDiffError, match="no diff_ref"):
        seat._leg_prompt("Review this", {}, "review", leg)
    assert seat._leg_prompt("Review this", {}, "review", leg, document_review=True) == "Review this"
    assert seat._leg_prompt("p", {}, "implement", leg) == "p"


def test_inline_only_plain_card_dry_run_path_parity(monkeypatch, tmp_path):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    from plangraph.spawner.service import SpawnService

    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == "mimo-v2.6-flash")
    plan = SpawnPlan((chosen,), (), "operator-order", (), ())
    monkeypatch.setattr(SpawnService, "_route", lambda *args, **kwargs: plan)

    async def no_probe(*args, **kwargs):
        return None

    monkeypatch.setattr("plangraph.spawner.leg_probe.before_launch", no_probe)
    card = {"estimate_tokens": 100}

    async def check():
        previews = []
        for name, factory in (("service", SpawnService), ("wrapper", spawn_service)):
            runner = SpawnerRunner(default_cwd=str(tmp_path))
            service = factory(runner=runner, state=tmp_path / name)
            preview = await service.spawn(card=card, prompt="Review this design", role="review",
                                          dry_run=True, wait_seconds=10)
            assert preview["status"] == "dry_run"
            assert preview["chosen_leg"]["id"] == chosen.id
            previews.append(preview["chosen_leg"])
            live = await service.spawn(card=card, prompt="Review this design", role="review",
                                       wait_seconds=10)
            assert live["chosen_leg"] is None
            assert len(live["attempts"]) == 1
            attempt = live["attempts"][0]
            assert attempt["leg"]["id"] == chosen.id
            assert attempt["status"] == "skipped"
            assert attempt["skip_reason"].startswith("inline_diff_failed:")
        assert previews[0] == previews[1]

    asyncio.run(check())
