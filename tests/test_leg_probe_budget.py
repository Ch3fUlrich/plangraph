"""W1: the probe budget inside a spawn and the admission window.

Pins the stuck-probe fixes: a spawn's probes share one budget below the
admission window, hung probes fail over leg by leg, a spawn joining an
in-flight probe is bounded and charged, and a backward wall-clock step never
restores budget. Split from test_leg_probe_w1.py to stay under the file-size gate.
"""
import asyncio
from dataclasses import replace
import os
import time
from types import SimpleNamespace

import pytest

from test_cargo_data_drive_target import isolated_cargo_lease_target  # noqa: F401
from test_leg_probe_w1 import _child_runner, _events, _leg


def exercise_local_probe(tmp_path, monkeypatch, *, code, isolated=False, cancel=False, contended=False, seed=True):
    from plangraph.routing_support.leg_state import LegStateStore
    from plangraph.spawner import host_ready, leg_probe, probe_cleanup, resolver

    runner = _child_runner(tmp_path, monkeypatch, 'raise AssertionError("process created")', 'claude')
    leg = SimpleNamespace(model='claude-haiku-5-5', harness='claude')
    store = LegStateStore(tmp_path / 'legs.sqlite')
    if seed:
        store.record_probe_pass(leg_probe.key(leg), caller='existing')
        before = _events(store)
    assert os.environ.get('CARGO_TARGET_DIR') == str(host_ready.shared_cargo_target())
    cargo_lease = host_ready.cargo_lease
    try_lock = host_ready._try_cargo_lock
    calls = []

    async def factory(*args, **kwargs):
        calls.append(args)
        raise AssertionError('pre-dispatch probe launched a child')

    runner.process_factory = factory
    spawn = runner.spawn

    async def review_spawn(**options):
        if isolated:
            options['extra_env'] = dict(options['extra_env'], CARGO_TARGET_DIR=str(tmp_path / 'review'))
        return await spawn(**options)

    runner.spawn = review_spawn

    async def exercise(holder):
        from threading import Event
        entered, release = asyncio.Event(), asyncio.Event()
        release_worker = Event()
        loop = asyncio.get_running_loop()

        async def prepare(*args, **kwargs):
            # Pre-dispatch failure must ignore even quota-shaped job diagnostics.
            for job in runner.jobs.values():
                job.stderr = 'HTTP 429: usage_limit: try again later'
            if code != 'cargo_lease_timeout' and not contended:
                entered.set()
                await release.wait()
            return None

        cleanup = probe_cleanup.cleanup
        async def settle(*args, **kwargs):
            holder.close()
            release.set()
            release_worker.set()
            await cleanup(*args, **kwargs)

        if code == 'cargo_lease_timeout':
            monkeypatch.setattr(host_ready, 'cargo_lease', lambda: cargo_lease(wait=0.025))
        else:
            monkeypatch.setattr(probe_cleanup, 'cleanup', settle)
        if contended:
            first_attempt = True

            def second_attempt_waits(path, exclusive):
                nonlocal first_attempt
                if not first_attempt:
                    loop.call_soon_threadsafe(entered.set)
                    assert release_worker.wait(timeout=2)
                first_attempt = False
                return try_lock(path, exclusive)

            monkeypatch.setattr(host_ready, '_try_cargo_lock', second_attempt_waits)
        monkeypatch.setattr(resolver, 'host_skip_reason_async', prepare)
        timeout = 2 if cancel or code == 'cargo_lease_timeout' else 0.25
        task = asyncio.create_task(leg_probe.probe(runner, leg, store=store, role='review', timeout_s=timeout))
        if cancel:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return
        return await task

    with cargo_lease(exclusive=True) as holder:
        result = asyncio.run(exercise(holder))
    assert calls == []
    if seed:
        assert _events(store) == before
    else:
        assert not store.path.exists()
    job, = runner.jobs.values()
    if cancel:
        assert job.status == 'cancelled'
    else:
        assert (result['ok'], result['infra'], result['state'], result['reason']) == (
            False, True, 'host-not-ready', code)
        assert result['local_fault']['contention_observed'] == (code == 'cargo_lease_timeout' or contended)
        assert str(tmp_path) not in str(result)
        assert job.status == 'failed' and job.end_time
        assert any(event.get('event') == code for event in job.events)
    monkeypatch.setattr(host_ready, 'cargo_lease', cargo_lease)
    monkeypatch.setattr(host_ready, '_try_cargo_lock', try_lock)
    with cargo_lease(exclusive=True):
        pass


@pytest.mark.parametrize('code', ['cargo_lease_timeout', 'probe_pre_dispatch_timeout'])
def test_local_probe_failure_does_not_book(tmp_path, monkeypatch, code):
    exercise_local_probe(tmp_path, monkeypatch, code=code)


def test_cancelled_probe_closes_dispatch_gate(tmp_path, monkeypatch):
    exercise_local_probe(tmp_path, monkeypatch, code='probe_pre_dispatch_timeout', isolated=True, cancel=True)


def test_cancellation_during_failed_cleanup_does_not_book(tmp_path, monkeypatch):
    from plangraph.routing_support.leg_state import LegStateStore
    from plangraph.spawner import leg_probe, probe_cleanup

    async def spawn(**kwargs):
        return SimpleNamespace(job_id='finished', status='completed', exit_code=0, stdout='OK')

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        cleanup = probe_cleanup.cleanup

        async def failed_cleanup(*args, **kwargs):
            entered.set()
            await release.wait()
            await cleanup(*args, **kwargs)
            raise RuntimeError('fixture cleanup failure')

        monkeypatch.setattr(probe_cleanup, 'cleanup', failed_cleanup)
        runner = SimpleNamespace(spawn=spawn, cancel=_cancel_finished)
        task = asyncio.create_task(leg_probe.probe(runner, leg, store=store, role='review'))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert str(cancelled.value.__cause__) == 'fixture cleanup failure'

    async def _cancel_finished(job_id):
        pass

    leg = _leg()
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_probe_pass('other', caller='existing')
    before = _events(store)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *args, **kwargs: '/bin/true')
    asyncio.run(exercise())
    assert _events(store) == before


@pytest.mark.parametrize('cancel', [False, True])
def test_abandoned_probe_lease_cannot_dispatch_after_release(tmp_path, monkeypatch, cancel):
    exercise_local_probe(tmp_path, monkeypatch, code='probe_pre_dispatch_timeout', contended=True, cancel=cancel)


@pytest.mark.parametrize('path', ['base', 'route', 'probe'])
def test_local_lease_refusal_persists_the_terminal_attempt(tmp_path, monkeypatch, path):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.routing_support.leg_state import LegStateStore
    from plangraph.spawner import host_ready, resolver
    from plangraph.spawner.runner import spawn_service
    from plangraph.spawner.service import SpawnService

    runner = _child_runner(tmp_path, monkeypatch, 'raise AssertionError("process created")', 'claude')
    async def forbidden(*args, **kwargs):
        pytest.fail('the process factory ran despite a busy Cargo target')
    runner.process_factory = forbidden
    haiku = next(leg for leg in DEFAULT_LEGS if leg.id == 'claude-haiku-5.5')
    legs = (replace(haiku, roles=('implement',)), DEFAULT_LEGS[1])
    plan = SpawnPlan(legs, (), 'test', (), ())
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_probe_pass('other', caller='existing')
    before = _events(store)
    factory = SpawnService if path == 'base' else spawn_service
    service = factory(runner=runner, state=tmp_path / 'runs-state', leg_reader=store)
    monkeypatch.setattr(service, 'admit_spawn', lambda *args, **kwargs: None)
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *args, **kwargs: None)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800' if path == 'probe' else '0')
    lease = host_ready.cargo_lease
    with lease(exclusive=True):
        monkeypatch.setattr(host_ready, 'cargo_lease', lambda: lease(wait=0.025))
        reply = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='test task',
                                          _plan=plan, wait_seconds=None))
    record = service.result(reply['run_id'])
    assert record['status'] == 'failed' and record['reason'] == 'cargo_lease_timeout', record.get('error')
    assert record['local_fault']['contention_observed'] is True
    assert record['attempts'][0]['skip_reason'] == 'cargo_lease_timeout'
    assert all(attempt['status'] == 'skipped' for attempt in record['attempts'][1:])
    assert len(runner.jobs) == 1 and not record.get('dispatch_started')
    assert _events(store) == before


def test_probe_budget_inside_a_spawn_is_below_the_admission_window(monkeypatch):
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.delenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', raising=False)
    budget = leg_probe.ProbeBudget()
    assert budget.timeout() == base_fresh.ADMISSION_TIMEOUT / 3
    budget.spend(time.monotonic() - 70)  # one probe used 70 s of the spawn's 90 s
    assert budget.timeout() == pytest.approx(base_fresh.ADMISSION_TIMEOUT / 2 - 70, abs=0.5)
    budget.spend(time.monotonic() - 30)
    assert budget.timeout() == 0
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', '5')
    assert leg_probe.ProbeBudget().timeout() == 5


def test_hung_probes_fail_over_leg_by_leg_inside_the_admission_window(monkeypatch):
    """Serial hung probes share one budget: failover, then a refusal, never base_admission_timeout."""
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', '180')
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda name, **kwargs: '/bin/true')
    now = 100.0
    clock = SimpleNamespace(monotonic=lambda: now, time=lambda: 0.0)
    monkeypatch.setattr(leg_probe, 'time', clock)
    # Advance the probe clock once the harness is running so the real timeout
    # and cleanup paths can run without waiting out each probe's full share.
    await_completion = leg_probe._await_completion
    timeouts = []

    async def reach_deadline(runner, job, deadline):
        nonlocal now
        timeouts.append(deadline - now)
        now = deadline
        await await_completion(runner, job, deadline)

    monkeypatch.setattr(leg_probe, '_await_completion', reach_deadline)

    class Runner:
        def __init__(self):
            self._tasks, self.jobs, self.cancelled = {}, {}, []

        async def spawn(self, **kwargs):
            job = SimpleNamespace(job_id='hung%d' % len(self.jobs), status='running', exit_code=None)
            self.jobs[job.job_id] = job
            self._tasks[job.job_id] = asyncio.ensure_future(asyncio.sleep(3600))  # the harness hangs
            return job

        async def cancel(self, job_id):
            self.cancelled.append(job_id)
            self.jobs[job_id].status = 'cancelled'
            self._tasks[job_id].cancel()

    runner = Runner()
    legs = [SimpleNamespace(model='agy/model%d' % n, harness='agy') for n in range(3)]

    async def run():
        budget = leg_probe.ProbeBudget()
        skips = []
        for leg in legs:  # what the runner does for each leg it tries
            skips.append(await leg_probe.before_launch(runner, leg, 'implement', False, budget))
        return skips

    skips = asyncio.run(run())
    assert skips == ['down: timeout', 'down: timeout', 'probe_budget_exhausted']
    window = base_fresh.ADMISSION_TIMEOUT
    assert timeouts == pytest.approx([window / 3, window / 2 - window / 3])
    assert runner.cancelled == ['hung0', 'hung1']  # hung harnesses are killed, not orphaned


@pytest.fixture
def probe_waits(monkeypatch):
    """Advance the probe clock by each requested wait and expire its shield."""
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.delenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', raising=False)
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', 0.9)  # 0.3 s per probe, 0.45 s in all
    monkeypatch.setattr(leg_probe, '_INFLIGHT', {})
    now = 0.0
    waits = []

    async def expire(pending, timeout):
        nonlocal now
        waits.append(timeout)
        now += timeout
        return await asyncio.wait_for(pending, timeout=0)

    monkeypatch.setattr(leg_probe, 'time', SimpleNamespace(monotonic=lambda: now, time=time.time))
    monkeypatch.setattr(leg_probe, 'asyncio', SimpleNamespace(
        TimeoutError=asyncio.TimeoutError, ensure_future=asyncio.ensure_future,
        shield=asyncio.shield, wait_for=expire,
    ))
    return waits


def test_a_spawn_joining_an_inflight_probe_is_bounded_and_charged(monkeypatch, probe_waits):
    """The joining spawn waits at most its own remaining budget, and that wait is charged."""
    from plangraph.spawner import leg_probe

    async def unexpected_probe(*args, **kwargs):
        pytest.fail('the joining spawn should share the in-flight probe')

    monkeypatch.setattr(leg_probe, 'probe', unexpected_probe)

    async def run():
        leg = _leg()
        pending = asyncio.get_running_loop().create_future()
        leg_probe._INFLIGHT[leg_probe.key(leg)] = pending
        budget = leg_probe.ProbeBudget()
        try:
            skips = [await leg_probe.before_launch(None, leg, 'implement', False, budget)
                     for _ in range(3)]
            assert not pending.done()
            return skips, budget.left
        finally:
            pending.cancel()

    skips, left = asyncio.run(run())
    assert skips == ['probe_budget_exhausted'] * 3
    assert probe_waits == pytest.approx([0.3, 0.15])
    assert left == pytest.approx(0)


def test_the_spawn_that_starts_a_probe_is_bounded_too(monkeypatch, probe_waits):
    """A probe stuck in setup or cleanup cannot hold its own spawn past the probe budget."""
    from plangraph.spawner import leg_probe

    async def stuck(runner, leg, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(leg_probe, 'probe', stuck)

    async def run():
        leg, budget = _leg(), leg_probe.ProbeBudget()
        skip = await leg_probe.before_launch(None, leg, 'implement', False, budget)
        pending = leg_probe._INFLIGHT[leg_probe.key(leg)]
        assert not pending.done()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        return skip, budget.left

    skip, left = asyncio.run(run())
    assert skip == 'probe_budget_exhausted'
    assert probe_waits == pytest.approx([0.35])  # 0.3 s per probe and 0.05 s of owner grace
    assert left == pytest.approx(0.1)


def test_probe_budget_is_net_of_the_admission_time_already_spent(monkeypatch):
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.delenv('PLANGRAPH_LEG_PROBE_TIMEOUT_S', raising=False)
    window = base_fresh.ADMISSION_TIMEOUT
    assert leg_probe.ProbeBudget().left == window / 2
    late = leg_probe.ProbeBudget(started=time.time() - (window - window / 6 - 10))  # 10 s left before the reserve
    assert late.timeout() == pytest.approx(10, abs=0.5)
    spent = leg_probe.ProbeBudget(started=time.time() - window)
    assert spent.timeout() == 0


def test_a_backward_wall_clock_step_does_not_restore_probe_budget(monkeypatch):
    from plangraph.spawner import base_fresh, leg_probe
    window = base_fresh.ADMISSION_TIMEOUT
    token = base_fresh.ADMISSION_STARTED.set(time.monotonic() - (window - window / 6 - 10))
    try:
        # The wall clock jumped back an hour: only the monotonic admission clock is trusted.
        monkeypatch.setattr(leg_probe.time, 'time', lambda: 1_000_000.0)
        budget = leg_probe.ProbeBudget(started=1_000_000.0 + 3600)
        assert budget.timeout() == pytest.approx(10, abs=0.5)
    finally:
        base_fresh.ADMISSION_STARTED.reset(token)
