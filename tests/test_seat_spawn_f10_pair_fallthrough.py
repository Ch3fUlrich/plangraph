"""Rule 3.66 (f10): a smart seat that ends without a verdict falls to the other smart legs, then both weak seats launch."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.planner import routing
from plangraph.planner.legs import REVIEW_REQUIRED
from plangraph.planner.seats import seat_family
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.service import SpawnService
from registry_route_util import _Runner

CARD = {'estimate_tokens': 1000, 'diff_ref': {'repo': '/repo', 'base': 'a1', 'head': 'b2'}}


class Runner(_Runner):
    def __init__(self, limited=()):
        super().__init__()
        self.limited = limited
        self.started = []

    async def spawn(self, **kwargs):
        job = await super().spawn(**kwargs)
        model = kwargs['model_or_combo']
        self.started.append(model)
        job.exit_code = 0
        job.stdout = 'FILES READ: src/lib.rs\nVERDICT: REQUEST_CHANGES\nTEST_GAMING: no\n'
        if any(part in model for part in self.limited):
            self.provider_responses[job.job_id] = {'status': 429, 'body': 'rate limited'}
        return job


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'catalog_rows', ())
    monkeypatch.setattr(SpawnService, '_leg_prompt', lambda *a, **k: 'review')
    monkeypatch.setattr('plangraph.planner.seats.head_writer_families', lambda card: [])   # no Git here
    store = SimpleNamespace(leg_status=lambda *a, **k: dict(state='ok', evidence=None))
    reader = SimpleNamespace(enabled=True, store=store, leg_status=store.leg_status,
                             record_probe_fail=lambda *a, **k: None)
    return lambda limited=(): spawn_service(Runner(limited), state=tmp_path / 'state', leg_reader=reader)


def seats_of(svc, writers=('gpt',)):
    return asyncio.run(svc.spawn(card=CARD, role='review', writer_families=list(writers),
                                 seats=True, wait_seconds=None, benchmark={}))


def test_a_smart_seat_with_a_verdict_is_the_only_seat(service):
    answer = seats_of(service())
    assert [r['chosen_leg']['id'] for r in answer['seats']] == ['agy-gemini-4-hydrogen-high']


def test_every_smart_leg_without_a_verdict_launches_both_weak_seats(service):
    svc = service(limited=('gemini-4',))   # hydrogen and both Argons end without a verdict
    answer = seats_of(svc)
    chosen = [r['chosen_leg']['id'] if r.get('chosen_leg') else None for r in answer['seats']]
    assert chosen[0] is None   # the smart slot is exhausted
    assert sorted(c for c in chosen if c) == sorted(REVIEW_REQUIRED)
    weak = [r for r in answer['seats'] if r.get('chosen_leg')]
    assert all(r['status'] == 'completed' for r in weak)
    assert len({seat_family(r['chosen_leg']['family']) for r in weak}) == 2


def test_a_smart_fallback_that_gives_a_verdict_never_launches_the_weak_pair(service):
    answer = seats_of(service(limited=('hydrogen',)))
    assert [r['chosen_leg']['id'] for r in answer['seats']] == ['agy-gemini-4-argon-low']


def running(leg, n):
    return {'runs': [{'role': 'review', 'status': 'running', 'chosen_leg': {'id': leg}}] * n}


@pytest.mark.parametrize('writers,leg,smart', [(('gpt',), 'agy-gemini-4-hydrogen-high', 'agy-gemini-4-hydrogen-high'),
                                               (('gemini',), 'claude-sonnet-5-5-high', 'claude-sonnet-5-5-high')])
def test_a_saturated_smart_leg_sends_the_head_to_the_weak_pair_through_the_service(
        service, monkeypatch, writers, leg, smart):
    from plangraph.planner.route_policy import DEFAULT_POLICY_PATH, load_route_policy
    monkeypatch.setenv('PLANGRAPH_ROUTE_POLICY', str(DEFAULT_POLICY_PATH))
    cap = load_route_policy().review_parallel_slots[leg]
    svc = service()
    free = svc.route(CARD, 'review', list(writers), benchmark={})
    assert [s.id for s in free.review_seats] == [smart]
    monkeypatch.setattr(svc, 'ps', lambda: running(leg, cap - 1))
    assert [s.id for s in svc.route(CARD, 'review', list(writers), benchmark={}).review_seats] == [smart]
    monkeypatch.setattr(svc, 'ps', lambda: running(leg, cap))
    full = svc.route(CARD, 'review', list(writers), benchmark={})
    assert [s.id for s in full.review_seats] == list(REVIEW_REQUIRED)
    assert {row['leg']: row['reason'] for row in full.skipped}[leg] == 'review_slots_busy'


@pytest.mark.parametrize('seats', [False, True], ids=['single', 'seat-group'])
def test_a_launched_seat_for_a_two_key_part_persists_both_keys_and_the_head(service, seats):
    import json
    keys = ['plangraph:LE-ONE', 'plangraph:LE-TWO']
    svc = service()
    answer = asyncio.run(svc.spawn(card=dict(CARD, item_keys=keys), role='review', writer_families=['gpt'],
                                   seats=seats, wait_seconds=None, benchmark={}))
    runs = answer['seats'] if seats else [answer]
    assert runs
    for run in runs:
        saved = json.loads(svc._path(run['run_id']).read_text())
        assert saved['item_key'] == keys[0] and saved['item_keys'] == keys
        assert saved['request']['review_head'] == 'b2'
