"""Scheduled review probe: a Codex usage-limit reply is a limited leg with its reset time,
not a failed leg (LE-REVIEW-PROBE-CODEX-USAGE-LIMIT-COUNTED-AS-FAILURE)."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from plangraph.routing_support.leg_state import LegStateStore

CODEX = 'gpt-6.2-sol-medium'


def _ordinal(day):
    return f"{day}{'th' if 11 <= day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')}"


def _spelled(when):
    return (f"{when.strftime('%b')} {_ordinal(when.day)}, {when.year} "
            f"{when.strftime('%I:%M %p').lstrip('0')}")


def _probe_one(tmp_path, monkeypatch, leg_id, spawn):
    from plangraph.planner import legs
    from plangraph.spawner import leg_probe

    leg = next(row for row in legs.routed_review_legs() if row.id == leg_id)
    monkeypatch.setattr(legs, 'routed_review_legs', lambda: (leg,))
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *args, **kwargs: '/bin/' + leg.harness)
    monkeypatch.setattr(leg_probe, '_check_manage_key', lambda runner: None)
    store = LegStateStore(tmp_path / 'legs.sqlite')

    async def cancel(job_id):
        pass

    runner = SimpleNamespace(leg_reader=store, spawn=spawn, cancel=cancel)
    reply = asyncio.run(leg_probe.probe_many(runner, all_reviewers=True, timeout=5))
    return leg, store, reply, runner


def _codex_runner(message, calls):
    async def spawn(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(job_id='j', status='failed', exit_code=1, stdout='', stderr=message)
    return spawn


def test_codex_usage_limit_is_limited_with_its_reset_time(tmp_path, monkeypatch):
    reset = (datetime.now() + timedelta(days=3)).replace(second=0, microsecond=0)
    message = (f"ERROR: You’ve hit your usage limit. Upgrade to Pro, visit "
               f"https://chatgpt.com/settings/usage to purchase more credits or try again at {_spelled(reset)}.")
    calls = []
    leg, store, reply, runner = _probe_one(tmp_path, monkeypatch, CODEX, _codex_runner(message, calls))
    assert reply['ok'] and reply['limited_legs'] == [CODEX]
    assert reply['probes'][0]['reason'] == 'limited'
    evidence = store.leg_status(CODEX)['evidence']
    assert store.leg_status(CODEX)['state'] == 'limited'
    assert evidence['status'] == 429 and abs(evidence['reset_at'] - reset.timestamp()) < 2
    assert reply['probes'][0]['reset_at'] == evidence['reset_at']
    # The next run skips the limited leg instead of spawning it again.
    from plangraph.spawner import leg_probe
    again = asyncio.run(leg_probe.probe_many(runner, all_reviewers=True, timeout=5))
    assert again['ok'] and again['probes'][0]['skipped'] == 'limited' and len(calls) == 1


def test_codex_usage_limit_without_reset_time_is_still_limited(tmp_path, monkeypatch):
    message = "ERROR: You've hit your usage limit. Visit https://chatgpt.com/settings/usage"
    calls = []
    _, store, reply, _ = _probe_one(tmp_path, monkeypatch, CODEX, _codex_runner(message, calls))
    assert reply['ok'] and reply['limited_legs'] == [CODEX]
    assert store.leg_status(CODEX)['state'] == 'limited'


@pytest.mark.parametrize('stderr,stdout', [
    ('', "the reviewer wrote: you've hit your usage limit"),
    ('retrying: note that you have hit your usage limit soon', ''),
    ('ERROR: stream disconnected before completion', ''),
])
def test_codex_prose_or_other_failure_is_not_a_limit(tmp_path, monkeypatch, stderr, stdout):
    async def spawn(**kwargs):
        return SimpleNamespace(job_id='j', status='failed', exit_code=1, stdout=stdout, stderr=stderr)
    _, store, reply, _ = _probe_one(tmp_path, monkeypatch, CODEX, spawn)
    assert not reply['ok'] and reply['limited_legs'] == []
    assert store.leg_status(CODEX)['state'] != 'limited'
