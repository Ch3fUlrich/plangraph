"""LE-SEAT-NO-VERDICT: a review seat's verdict is its stdout (D-3226).

A review run that ends without a ``VERDICT: PASS|FAIL`` line is failed/no_verdict,
never 'completed'; the mimo seats never take a review run at all (D-3225).
"""
import asyncio
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, NO_VERDICT_SEATS
from plangraph.planner.routing import SpawnPlan, SpawnRefused
from plangraph.spawner.runner import JobInfo, spawn_service
from plangraph.spawner.service import SpawnService

CARD = {'estimate_tokens': 1000}


class _Runner:
    def __init__(self, stdout=''):
        self.jobs, self._processes, self._tasks, self.provider_responses = {}, {}, {}, {}
        self.calls, self.stdout = [], stdout

    async def spawn(self, **kw):
        from plangraph.spawner.runner import JobInfo
        self.calls.append(kw)
        job = JobInfo('job-%d' % len(self.calls), 'completed', kw['prompt'], kw['model_or_combo'],
                      exit_code=0, stdout=self.stdout)
        self.jobs[job.job_id] = job
        return job


def _run(tmp_path, role, stdout):
    service = SpawnService(_Runner(stdout), state=tmp_path)
    return asyncio.run(service.spawn(card=CARD, prompt='task', role=role,
                                     leg='qodercli-qwen3.8-flash', writer_families=['claude'],
                                     override_reason='no-verdict contract test', wait_seconds=None))


def test_review_without_verdict_fails_no_verdict(tmp_path):
    record = _run(tmp_path, 'review', 'looks good to me')
    assert record['status'] == 'failed' and record['exit_code'] == 1
    assert record['reason'] == 'no_verdict'
    assert {'event': 'no_verdict'} in record['events']
    assert record['error'] == 'no_verdict'


def test_review_with_verdict_pass_stays_completed(tmp_path):
    record = _run(tmp_path, 'review', 'analysis\n\nVERDICT: PASS\n')
    assert record['status'] == 'completed' and record['exit_code'] == 0
    assert record.get('reason') is None
    assert 'no_verdict' not in str(record.get('events'))


def test_implement_without_verdict_is_unaffected(tmp_path):
    record = _run(tmp_path, 'implement', 'shipped; no verdict line here')
    assert record['status'] == 'completed' and record.get('reason') is None


def test_review_leg_on_mimo_flash_is_refused(tmp_path):
    with pytest.raises(SpawnRefused, match='no-verdict seat'):
        asyncio.run(SpawnService(state=tmp_path).spawn(
            card=CARD, prompt='x', role='review', leg='mimo-v2.6-flash',
            writer_families=['claude'], override_reason='no-verdict contract test',
            dry_run=True))


def test_route_skips_mimo_for_review(monkeypatch):
    from plangraph.planner import routing
    from plangraph.planner.legs import Leg
    monkeypatch.setattr(routing, 'validate_model', lambda model: None)
    for model in ('mimo/mimo-v2.6-flash',):
        assert model in NO_VERDICT_SEATS
        seat = Leg('nv', 'subscription', 'opencode', model, 'mimo', ('128k',), 200000, False,
                   roles=('review',))
        plan = routing.plan_spawn(CARD, role='review', writer_families=('claude',), legs=(seat,),
                                  leg_reader=lambda *a, **kw: {'state': 'available', 'evidence': None})
        assert not plan.legs
        assert plan.skipped[0]['reason'] == 'no_verdict_seat'


def test_review_leg_on_mimo_pro_warns(tmp_path, caplog):
    with caplog.at_level('WARNING'):
        asyncio.run(SpawnService(state=tmp_path).spawn(
            card=CARD, prompt='x', role='review', leg='mimo-v2.6-pro',
            writer_families=['claude'], override_reason='review-unfit warn test',
            dry_run=True))
    assert 'review-unfit seat model' in caplog.text


# --- both entry points: SpawnService and the production spawn_service path ---


SEAT_LEGS = tuple(replace(DEFAULT_LEGS[0], id='nv-' + n, model='test/seat-' + n)
                  for n in ('a', 'b'))


class _SeatRunner:
    """Fake runner answering one scripted JobInfo per launched seat."""

    def __init__(self, *jobs):
        self.script, self.calls, self.jobs = list(jobs), [], {}
        self._processes, self._tasks, self.provider_responses = {}, {}, {}

    async def spawn(self, **kw):
        self.calls.append(kw)
        job = self.script.pop(0)
        self.jobs[job.job_id] = job
        return job


def _routed(factory, runner, tmp_path, monkeypatch, legs=SEAT_LEGS):
    from plangraph.spawner import resolver
    service = factory(runner=runner, state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan(legs, (), 'test', (), ()))
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    return service


def _spawn(service):
    return asyncio.run(service.spawn(card=CARD, prompt='task', role='review',
                                     writer_families=['claude'], wait_seconds=None))


@pytest.mark.parametrize('factory', [SpawnService, spawn_service])
def test_no_verdict_seat_launches_next_leg_and_later_pass_completes(tmp_path, monkeypatch, factory):
    # D-3226/D-3427: the verdictless first seat fails/no_verdict and falls
    # through to the next leg; a later seat's PASS ends the run completed.
    runner = _SeatRunner(
        JobInfo('job-1', 'completed', '', 'test/seat-a', role='review',
                exit_code=0, stdout='looks good to me\n'),
        JobInfo('job-2', 'completed', '', 'test/seat-b', role='review',
                exit_code=0, stdout='reviewed the flush fix\n\nVERDICT: PASS\n'))
    record = _spawn(_routed(factory, runner, tmp_path, monkeypatch))
    assert [kw['model_or_combo'] for kw in runner.calls] == [leg.model for leg in SEAT_LEGS]
    assert runner.calls[0]['watchdog']['max_s'] == 30 * 60   # the seat cap travels both paths
    assert record['status'] == 'completed' and record['exit_code'] == 0
    assert record['attempts'][0]['status'] == 'failed'        # no VERDICT line
    assert record['attempts'][0]['skip_reason'] == 'no_verdict'
    assert record['attempts'][1]['status'] == 'completed'
    assert record.get('reason') is None                       # the later PASS wins


@pytest.mark.parametrize('factory', [SpawnService, spawn_service])
@pytest.mark.parametrize('hung,exit_code,rule', [('timeout', 124, 'max'),
                                                 ('stalled', 6, 'no-progress')])
def test_hung_review_seat_falls_through_and_keeps_watchdog_evidence(
        tmp_path, monkeypatch, factory, hung, exit_code, rule):
    # D-3427: a hung review seat (cap or stall) falls through to the next leg,
    # keeps its real status in the attempt row, and leaves its watchdog
    # evidence on the run record.
    event = {'event': 'watchdog_timeout' if hung == 'timeout' else 'watchdog_stalled',
             'rule': rule}
    runner = _SeatRunner(
        JobInfo('job-1', hung, '', 'test/seat-a', role='review',
                exit_code=exit_code, stdout='started the review\n', events=[event]),
        JobInfo('job-2', 'completed', '', 'test/seat-b', role='review',
                exit_code=0, stdout='VERDICT: PASS\n'))
    record = _spawn(_routed(factory, runner, tmp_path, monkeypatch))
    assert [kw['model_or_combo'] for kw in runner.calls] == [leg.model for leg in SEAT_LEGS]
    assert record['status'] == 'completed'
    assert record['attempts'][0]['status'] == hung             # the hung seat keeps its status
    assert record['attempts'][0]['skip_reason'] == hung        # ...and falls through on it
    assert record['watchdog'] == event                         # evidence persisted
    assert record['attempts'][1]['status'] == 'completed'


@pytest.mark.parametrize('factory', [SpawnService, spawn_service])
def test_routed_review_unfit_model_warns(tmp_path, monkeypatch, factory, caplog):
    # D-3422: a routed review seat on mimo-v2.6-pro warns before launch.
    runner = _SeatRunner(JobInfo('job-1', 'completed', '', 'mimo/mimo-v2.6-pro',
                                 role='review', exit_code=0, stdout='VERDICT: PASS\n'))
    legs = (replace(DEFAULT_LEGS[0], id='pro', model='mimo/mimo-v2.6-pro'),)
    with caplog.at_level('WARNING'):
        record = _spawn(_routed(factory, runner, tmp_path, monkeypatch, legs=legs))
    assert 'review-unfit seat model (D-3422): mimo/mimo-v2.6-pro' in caplog.text
    assert record['status'] == 'completed'


@pytest.mark.parametrize('factory', [SpawnService, spawn_service])
def test_routed_no_verdict_model_is_refused_before_launch(tmp_path, monkeypatch, factory):
    # D-3225: a routed review seat on a no-verdict model is refused, never launched.
    runner = _SeatRunner()
    legs = (replace(DEFAULT_LEGS[0], id='flash', model='mimo/mimo-v2.6-flash'),)
    record = _spawn(_routed(factory, runner, tmp_path, monkeypatch, legs=legs))
    assert record['status'] == 'refused' and record['exit_code'] == 2
    assert 'no-verdict seat model (D-3225)' in record['error']
    assert runner.calls == []


def test_review_fail_verdict_is_a_decision_not_no_verdict(tmp_path):
    # 'VERDICT: FAIL - 2 findings' parses (as fix): a review's decision is a
    # completed seat; only a verdictless run is failed/no_verdict.
    record = _run(tmp_path, 'review', '2 blocking findings\n\nVERDICT: FAIL - 2 findings\n')
    assert record['status'] == 'completed'
    assert 'no_verdict' not in str(record.get('events'))


def test_echoed_template_verdict_is_no_verdict(tmp_path):
    record = _run(tmp_path, 'review', 'checks done\n\nVERDICT: pass | fix | halt\n')
    assert record['status'] == 'failed' and record['reason'] == 'no_verdict'


def test_ansi_wrapped_verdict_counts(tmp_path):
    esc = chr(27)
    record = _run(tmp_path, 'review', esc + '[1;32mVERDICT: PASS' + esc + '[0m\n')
    assert record['status'] == 'completed' and record.get('reason') is None
