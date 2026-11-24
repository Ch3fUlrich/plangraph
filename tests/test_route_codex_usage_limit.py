"""A Codex 'usage limit' reply marks every Codex leg limited_until the reset (plangraph:LE-ROUTE-SERVES-CODEX-WHILE-USAGE-LIMITED)."""
import calendar
import time
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.env_redact import harness_limit_line

REPLY = ("ERROR: You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
         "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at "
         "Nov 27th, 2026 8:24 PM.")
RESET = calendar.timegm((2026, 11, 27, 20, 24, 0))   # the reply's wall time read as UTC
CARD = {"estimate_tokens": 400_000}


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def codex_models():
    return {leg.model for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS) if leg.harness == "codex"}


def test_the_codex_reply_is_recognised_as_limit_evidence():
    assert harness_limit_line(1, "", REPLY) == REPLY
    assert harness_limit_line(1, "", "Error: You've hit your usage limit. Try again later.") is not None
    # Prose that mentions the phrase mid-line is not evidence (D-3257).
    assert harness_limit_line(1, "", "the docs say you've hit your usage limit when busy") is None


def test_the_reset_time_is_parsed_from_the_reply(tmp_path):
    store = LegStateStore(tmp_path / "legs.sqlite")
    now = RESET - 3600
    store.record_harness_limit("omniroute/cx-gpt-6.2-sol-medium", REPLY, "spawner:harness", now=now, harness="codex")
    status = store.leg_status("omniroute/cx-gpt-6.2-sol-medium", now=now + 1, harness="codex")
    assert status["state"] == "limited" and status["evidence"]["reset_at"] == RESET
    assert store.leg_status("omniroute/cx-gpt-6.2-sol-medium", now=RESET + 1, harness="codex")["state"] == "ok"


def test_a_booked_run_limit_limits_every_codex_leg_account_wide(tmp_path):
    from plangraph.spawner.exit_outcome import _book_failure
    store = LegStateStore(tmp_path / "legs.sqlite")
    service = SimpleNamespace(leg_reader=store)
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == "cx-gpt-6.2-sol-medium")
    _book_failure(service, chosen, REPLY, {})
    for model in codex_models():
        assert store.leg_status(model, now=time.time(), harness="codex")["state"] == "limited", model


def test_the_route_skips_the_cx_legs_as_limited_until_and_admits_sonnet(tmp_path):
    store = LegStateStore(tmp_path / "legs.sqlite")
    now = RESET - 3600
    for model in codex_models():
        store.record_harness_limit(model, REPLY, "spawner:harness", now=now, harness="codex")
    plan = plan_spawn(CARD, role="implement", leg_reader=store, now=now + 60, catalog_rows=())
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(RESET))
    skipped = {s["leg"]: s["reason"] for s in plan.skipped}
    assert skipped["cx-gpt-6.2-sol-medium"] == f"limited_until {reset}"
    assert plan.first_leg.id == "claude-sonnet-5-5-high"
    assert "claude_requires_codex_limited" not in skipped.values() or all(
        leg_id != "claude-sonnet-5-5-high" for leg_id, why in skipped.items() if why == "claude_requires_codex_limited")
    assert not any(leg.harness == "codex" for leg in plan.legs)
    after = plan_spawn(CARD, role="implement", leg_reader=store, now=RESET + 60, catalog_rows=())
    assert after.first_leg.id == "cx-gpt-6.2-sol-medium"


def test_a_failed_probe_with_the_reply_books_the_limit():
    from plangraph.spawner.leg_probe import probe_quota_line
    codex = SimpleNamespace(harness="codex")
    for review_mode in (False, True):   # a writer probe and a review probe
        assert probe_quota_line(codex, review_mode, 7, "", REPLY) == REPLY
        assert probe_quota_line(codex, review_mode, 7, "", "boom") is None
    # A writer probe of any other harness books nothing (unchanged).
    assert probe_quota_line(SimpleNamespace(harness="opencode"), False, 7, "", REPLY) is None


def test_a_codex_probe_that_exits_7_with_the_reply_limits_every_codex_leg_and_is_named(tmp_path, monkeypatch):
    """The incident: a probe exited 7 in seconds and the route read no limited_legs entry."""
    import asyncio
    import datetime
    import subprocess
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import spawn_service
    leg = next(l for l in DEFAULT_LEGS if l.id == "cx-gpt-6.2-sol-medium")
    store = LegStateStore(tmp_path / "legs.sqlite")
    runner = SimpleNamespace(leg_reader=store, jobs={})
    spawn_service(runner, state=tmp_path, leg_reader=store)
    until = (datetime.datetime.now() + datetime.timedelta(days=2)).replace(second=0, microsecond=0)
    reply = (f"ERROR: You've hit your usage limit. Upgrade to Pro or try again at "
             f"{until:%b} {until.day}th, {until:%Y} {until:%I}:{until:%M} {until:%p}.")

    async def spawn(**kwargs):
        return SimpleNamespace(job_id="probe", status="failed", exit_code=7, stdout="", stderr=reply)

    async def cancel(job_id):
        pass
    runner.spawn, runner.cancel = spawn, cancel
    root = tmp_path / "probe"
    root.mkdir()
    monkeypatch.setattr(leg_probe.shutil, "which", lambda *a, **kw: "fake")
    monkeypatch.setattr(leg_probe, "caller_heads", lambda: {})
    monkeypatch.setattr(leg_probe.tempfile, "mkdtemp", lambda **kw: str(root))
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: b"initial")
    result = asyncio.run(leg_probe.probe_many(runner, leg.id))
    assert result["limited_legs"] == [leg.id] and result["ok"] is True
    assert result["probes"][0]["reset_at"] == time.mktime(until.timetuple())
    for model in codex_models():
        status = store.leg_status(model, harness="codex")
        assert status["state"] == "limited" and status["evidence"]["reset_at"] == time.mktime(until.timetuple()), model
    plan = plan_spawn(CARD, role="implement", leg_reader=store, catalog_rows=())
    assert plan.first_leg.id == "claude-sonnet-5-5-high"
