"""D-3622 slice 1: a run with no first event within first_event_s ends failed as hung."""
import asyncio
import math
import sys
import time
from types import SimpleNamespace

import pytest

from plangraph.planner.routing import SpawnRefused
from plangraph.spawner.run_activity import hang_event
from plangraph.spawner.run_watchdog import collect, thresholds
from tests.test_run_watchdog import _job

LIMITS = {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30, 'first_event_s': 2}
BASE = {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30}
SILENT = "import time\ntime.sleep(30)\n"
SHORT_SILENT = "import time\ntime.sleep(3)\n"
SPEAKS = "import time\nprint('starting work', flush=True)\ntime.sleep(30)\n"


def _spawn(tmp_path, script):
    return asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)


def test_silent_writer_fails_as_hung_within_8s(tmp_path):
    # No first event within 2 s (card first_event_s): failed/8 now, with the hang event.
    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await _spawn(tmp_path, SILENT)
        started = time.time()
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert time.time() - started < 8          # the child sleeps 30 s: it was stopped, not awaited
        assert job.status == 'failed' and job.exit_code == 8
        hangs = [e for e in job.events if e['event'] == 'hang_no_first_event']
        assert hangs and hangs[0]['line'] == 'hang_no_first_event: no harness event within 2 s of launch'
        assert proc.returncode is not None
    asyncio.run(run())


def test_first_event_at_1s_is_not_hung(tmp_path):
    # A run that prints anything before the limit is unaffected.
    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await _spawn(tmp_path, SPEAKS)
        task = asyncio.ensure_future(collect(runner, 'j', proc))
        await asyncio.sleep(2.5)                   # past the 2 s first-event limit
        assert not task.done() and job.status == 'running'
        assert not [e for e in job.events if e['event'] == 'hang_no_first_event']
        proc.kill()
        await asyncio.wait_for(task, timeout=10)
        assert job.status == 'running'
        assert not [e for e in job.events if e['event'] == 'hang_no_first_event']
    asyncio.run(run())


def test_review_role_silent_child_exempt(tmp_path):
    # Only writers are held to the first-event rule; a review seat may buffer.
    async def run():
        runner, job = _job(tmp_path, LIMITS, role='review')
        proc = await _spawn(tmp_path, SHORT_SILENT)
        task = asyncio.ensure_future(collect(runner, 'j', proc))
        await asyncio.sleep(2.5)                   # past the 2 s limit: no writer rule for review
        assert not task.done() and job.status == 'running'
        await asyncio.wait_for(task, timeout=10)
        assert job.status == 'running'
        assert not [e for e in job.events if e['event'] == 'hang_no_first_event']
    asyncio.run(run())


@pytest.mark.parametrize('value', [-1, True, '2', math.inf, math.nan])
def test_invalid_card_first_event_refused(value):
    with pytest.raises(SpawnRefused):
        thresholds({'first_event_s': value})


def test_card_first_event_zero_turns_the_rule_off():
    limits = thresholds({'first_event_s': 0})
    assert limits['first_event_s'] is None and limits['first_event_card'] is True


def test_invalid_env_first_event_refused(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_FIRST_EVENT_S', 'x')
    with pytest.raises(SpawnRefused):
        thresholds({})


def test_env_zero_turns_the_rule_off(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_FIRST_EVENT_S', '0')

    async def run():
        runner, job = _job(tmp_path, BASE)          # no card first_event_s: the env rule applies
        assert job._watchdog_limits['first_event_s'] is None
        proc = await _spawn(tmp_path, SHORT_SILENT)
        task = asyncio.ensure_future(collect(runner, 'j', proc))
        await asyncio.sleep(2.5)                   # past what would be the 2 s limit
        assert not task.done() and job.status == 'running'
        await asyncio.wait_for(task, timeout=10)
        assert not [e for e in job.events if e['event'] == 'hang_no_first_event']
    asyncio.run(run())


def test_default_first_event_is_90s(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_FIRST_EVENT_S', raising=False)
    limits = thresholds({})
    assert limits['first_event_s'] == 90.0 and limits['first_event_card'] is False
    assert thresholds({'first_event_s': 2})['first_event_card'] is True


def test_hung_leg_booked_down_for_hang_down_s(tmp_path, monkeypatch):
    # The hung leg is booked down like a refused one (hang_down_s, PLANGRAPH_LEG_HANG_DOWN_S).
    from plangraph.routing_support.leg_state import LegStateStore
    from plangraph.spawner.exit_outcome import fallthrough_reason
    monkeypatch.setenv('PLANGRAPH_LEG_HANG_DOWN_S', '123')
    store = LegStateStore(tmp_path / 'legs.sqlite')
    job = SimpleNamespace(exit_code=8, stdout='', stderr='', events=[hang_event(90.0, 90.1)])
    record = {}
    reason = fallthrough_reason(SimpleNamespace(leg_reader=store),
                                SimpleNamespace(model='m', harness='opencode'), job, record)
    assert reason is None
    assert record['reason'] == 'hang_no_first_event: no harness event within 90 s of launch'
    state = store.leg_status('m')
    assert state['state'] == 'limited'
    assert time.time() + 100 < state['evidence']['reset_at'] < time.time() + 200
