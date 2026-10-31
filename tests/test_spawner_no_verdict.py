"""LE-SEAT-NO-VERDICT: a review seat's verdict is its stdout (D-3226).

A review run that ends without a ``VERDICT: PASS|FAIL`` line is failed/no_verdict,
never 'completed'; the mimo seats never take a review run at all (D-3225).
"""
import asyncio

import pytest

from plangraph.planner.legs import NO_VERDICT_SEATS
from plangraph.planner.routing import SpawnRefused
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
