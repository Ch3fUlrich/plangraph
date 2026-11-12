"""Synthetic metadata-only replay; no live acceptance or token savings claim."""
import builtins
import asyncio
import io
import socket
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.spawner import run_watchdog as wd


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    monkeypatch.setattr(wd.os, 'environ', {})
    def denied(*args, **kwargs):
        raise AssertionError('external access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    monkeypatch.setattr(builtins, 'open', denied)
    monkeypatch.setattr(io, 'open', denied)


def limits(enabled=True):
    return wd.thresholds(dict(stall_min=1, first_edit_min=2, max_min=3,
                              hard_first_edit=enabled))


def test_continuous_output_replay(monkeypatch, record_property):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(wd.time, 'time', lambda: clock.now)
    elapsed = []
    for enabled in (False, True):
        w = wd.Watchdog(limits(enabled), 0, ('files', 'head'))
        for second in range(301):
            clock.now = second
            event = w.decide(wd.time.time(), ('files', 'head'), second, second)
            if event:
                elapsed.append(second)
                assert event['rule'] == ('no-first-edit' if enabled else 'max')
                break
    assert elapsed == [300, 120]
    record_property('synthetic_legacy_seconds', elapsed[0])
    record_property('synthetic_enabled_seconds', elapsed[1])
    record_property('tokens', 'unknown')  # no fabricated token counts


def test_edit_switches_to_progress_rules():
    w = wd.Watchdog(limits(), 0, 'baseline')
    assert w.decide(119, 'changed', 119, 1) is None
    assert w.decide(120, 'changed', 120, 2) is None
    assert w.decide(180, 'changed', 120, 2)['rule'] == 'max'
    w = wd.Watchdog(limits(), 0, 'baseline')
    assert w.decide(1, 'changed', 1, 1) is None
    assert w.decide(61, 'changed', 1, 1)['rule'] == 'no-progress'


def test_unknown_baseline_and_missing_tick_are_not_no_edit():
    w = wd.Watchdog(limits(), 0, None)
    assert w.decide(120, None, 120, None) is None
    assert w.decide(121, 'adopted', 121, None) is None
    event = w.decide(300, None, 300, None)
    assert event['rule'] == 'max'
    assert event['edit_evidence'] == 'unknown'
    w = wd.Watchdog(limits(), 0, 'baseline')
    assert w.decide(120, None, 120, None) is None


def test_wall_cap_and_nonwriters_and_disabled_next_tick():
    cap = dict(limits(), hard_max=True, max_s=100)
    assert wd.Watchdog(cap, 0, 'b').decide(100, 'b', 100, None)['rule'] == 'max'
    for writer in (False, True):
        default = wd.Watchdog(wd.thresholds(dict(stall_min=1, first_edit_min=2,
                                               max_min=3)), 0, 'b', writer)
        disabled = wd.Watchdog(limits(False), 0, 'b', writer)
        for now in (1, 119, 120, 121, 180, 300):
            assert default.budget() == disabled.budget()
            assert default.decide(now, 'b', now, None) == disabled.decide(now, 'b', now, None)
    seat = wd.Watchdog(limits(), 0, 'b', writer=False)
    assert seat.decide(120, 'b', None, None) is None
    assert seat.decide(180, 'b', None, None)['rule'] == 'max'


def test_edit_signature_injected_subprocess(monkeypatch):
    monkeypatch.setattr(wd.os, 'walk', lambda cwd: iter([('tree', [], [])]))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=b'synthetic-head'))
    assert wd.edit_signature('tree') == ((0, 0, 0), b'synthetic-head')


def test_collect_enforces_deadline_with_injected_transport(monkeypatch):
    from plangraph.spawner.runner import JobInfo
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(wd.time, 'time', lambda: clock.now)
    monkeypatch.setattr(wd, 'edit_signature', lambda cwd: 'baseline')
    async def cpu(*args):
        return None
    monkeypatch.setattr(wd, 'cpu_seconds', cpu)
    async def run():
        job = JobInfo('j', 'running', '', 'm', role='implement', start_time=0)
        job._watchdog_limits = dict(limits(), interval=200)
        job._watchdog_baseline = 'baseline'
        stopped = asyncio.Event()
        class Transport:
            async def communicate(self, input=None):
                await stopped.wait()
                return b'synthetic output', b''
        async def reap(*args):
            stopped.set()
        runner = SimpleNamespace(jobs={'j': job}, _child_envs={'j': {}},
                                 _stdin_payloads={}, default_cwd='tree', _reap_process=reap)
        real_wait = asyncio.wait
        async def tick(waiters, timeout, return_when):
            assert timeout == 120
            clock.now += timeout
            job.last_activity_ts = clock.now
            return set(), set(waiters)
        monkeypatch.setattr(wd.asyncio, 'wait', tick)
        await wd.collect(runner, 'j', Transport())
        monkeypatch.setattr(wd.asyncio, 'wait', real_wait)
        assert clock.now == 120
        assert job.status == 'stalled'
        assert job.events[0]['rule'] == 'no-first-edit'
        assert stopped.is_set()
    asyncio.run(run())


@pytest.mark.parametrize('value', [1, 'true', None])
def test_invalid_opt_in(value):
    from plangraph.planner.routing import SpawnRefused
    with pytest.raises(SpawnRefused):
        wd.thresholds({'hard_first_edit': value})
