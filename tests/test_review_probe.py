"""Scheduled review probes use answer capability and persist routing admission."""
import asyncio
from types import SimpleNamespace

import pytest
from plangraph.spawner import leg_probe
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.routing_support.leg_state import LegStateStore


@pytest.mark.parametrize('answer,ok', [('', False), ('OK', True)])
def test_review_probe_books_answer_failure(tmp_path, monkeypatch, answer, ok):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda _: '/fake/harness')
    leg = next(l for l in DEFAULT_LEGS if l.id == 'mimo-v2.6-pro')
    calls = []
    class Runner:
        jobs = {}
        _tasks = {}
        async def spawn(self, **kw):
            calls.append(kw)
            job = SimpleNamespace(job_id='probe', status='completed', exit_code=0, stdout=answer, stderr='')
            self.jobs['probe'] = job
            return job
        async def cancel(self, jid):
            pass
    state = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(Runner(), leg, store=state, role='review'))
    assert result['ok'] is ok
    assert calls[0]['prompt'] == 'Reply with exactly OK.'
    assert calls[0]['role'] == 'review'
    assert calls[0]['reasoning_cap'] is True
    assert (state.leg_status(leg.model)['state'] == 'down') is (not ok)
    from plangraph.planner.routing import plan_spawn
    from plangraph.planner.route_policy import RoutePolicy
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'https://gateway.example.invalid/v1')
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'fixture-key')
    plan = plan_spawn({'estimate_tokens': 100, 'diff_ref': {
        'repo': str(tmp_path), 'base': 'HEAD', 'head': 'HEAD'}}, role='review',
        writer_families=['openai'], legs=[leg], catalog_rows=[],
        policy=RoutePolicy(), leg_reader=state)
    assert bool(plan.legs) is ok
