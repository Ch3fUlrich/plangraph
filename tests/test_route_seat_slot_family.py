"""D-5520: seat slots describe the resolved leg, not their initial family."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plangraph.planner import routing
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.seats import seat_family
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.service import SpawnService
from registry_route_util import _Runner


def leg(name):
    return next(row for row in DEFAULT_LEGS if row.id == name)


GEMINI = leg('agy-gemini-3.8-flash')
LUNA = leg('cx-gpt-6-luna')
SONNET = leg('agy-sonnet-5.5')
OPUS = leg('agy-opus-5.5')
CARD = {'estimate_tokens': 1000,
        'diff_ref': {'repo': '/repo', 'base': 'a1', 'head': 'b2'}}


class Runner(_Runner):
    def __init__(self, skip=None):
        super().__init__()
        self.skip = skip

    async def spawn(self, **kwargs):
        job = await super().spawn(**kwargs)
        job.exit_code = 0
        job.stdout = 'VERDICT: PASS\nTEST_GAMING: no\n'
        if kwargs['model_or_combo'] in ((self.skip,) if isinstance(self.skip, str) else self.skip or ()):
            self.provider_responses[job.job_id] = {'status': 429, 'body': 'rate limited'}
        return job


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (GEMINI, LUNA, SONNET, OPUS))
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'catalog_rows', ())
    # Exercise the real seat planner and dispatch, without Git, probes or a child.
    monkeypatch.setattr(SpawnService, '_leg_prompt', lambda *a, **k: 'review')
    store = SimpleNamespace(leg_status=lambda *a, **k: dict(state='ok', evidence=None))
    reader = SimpleNamespace(enabled=True, store=store, leg_status=store.leg_status,
                             record_probe_fail=lambda *a, **k: None)

    def make(dispatch=True, skip=None):
        return (spawn_service if dispatch else SpawnService)(
            Runner(skip), state=tmp_path / 'state', leg_reader=reader)
    return make


@pytest.mark.parametrize('dispatch', [False, True], ids=['base', 'dispatch'])
def test_resolved_slot_family_in_answer_and_record(service, dispatch):
    svc = service(dispatch, GEMINI.model)
    before = svc.route(CARD, 'review', ['mimo'], benchmark={})
    assert before.review_seats == (GEMINI, LUNA)
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    families = []
    for run in answer['seats']:
        saved = json.loads(svc._path(run['run_id']).read_text())
        assert run['status'] == saved['status'] == 'completed'
        for record in (run, saved):
            chosen = record['chosen_leg']
            assert record['plan']['review_seats'] == [chosen]
            own, = record['plan']['seat_coverage']
            assert own['leg'] == chosen['id']
            assert own['family'] == chosen['family']
            assert own['keeper_family'] == seat_family(chosen['family'])
        families.append(seat_family(run['chosen_leg']['family']))
        slot = next(row for row in answer['seat_coverage'] if row['leg'] == run['chosen_leg']['id'])
        assert slot['family'] == run['chosen_leg']['family']
    assert len(families) == len(set(families)) == 2
    assert 'anthropic' in families
    failed = next(run for run in answer['seats'] if run['chosen_leg']['id'] == SONNET.id)
    attempt = next(a for a in failed['attempts'] if isinstance(a['leg'], dict)
                   and a['leg']['id'] == GEMINI.id)
    assert attempt['skip_reason'] == 'provider_stop'


def test_unknown_adapter_skip_has_named_reason(service):
    svc = service()
    svc.runner.leg_reader.leg_status = lambda model, **k: (
        dict(state='limited', reason='unknown', evidence=None)
        if model in (GEMINI.model, 'agy/' + GEMINI.model)
        else dict(state='ok', evidence=None))
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    run = next(r for r in answer['seats'] if any(
        isinstance(a['leg'], dict) and a['leg']['id'] == GEMINI.id for a in r['attempts']))
    saved = json.loads(svc._path(run['run_id']).read_text())
    for record in (run, saved):
        attempt = next(a for a in record['attempts'] if isinstance(a['leg'], dict)
                       and a['leg']['id'] == GEMINI.id)
        assert attempt['skip_reason'] == 'leg_state_snapshot_error'
        assert record['plan']['review_seats'] == [record['chosen_leg']]


def test_unfilled_slot_names_distinct_family_refusal(service, monkeypatch):
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (GEMINI, LUNA))
    svc = service(skip=GEMINI.model)
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    filled = [r for r in answer['seats'] if r['chosen_leg']]
    assert len(filled) == 1 and filled[0]['chosen_leg']['id'] == LUNA.id
    unfilled, = [r for r in answer['seats'] if not r['chosen_leg']]
    assert 'distinct_family_unavailable' in unfilled['error']
    assert 'provider_stop' in unfilled['error']
    assert unfilled['plan']['review_seats'] == []
    assert 'distinct_family_unavailable' in '\n'.join(answer['reasons'])
    assert json.loads(svc._path(unfilled['run_id']).read_text())['error'] == unfilled['error']
    svc._save(unfilled)
    assert svc.result(unfilled['run_id'])['error'] == unfilled['error']


def test_route_names_anthropic_second_family_without_reordering(service):
    svc = service()
    plan = svc.route(CARD, 'review', ['gpt'], benchmark={})
    assert plan.review_seats == (GEMINI, SONNET)
    assert [l.id for l in plan.legs] == [GEMINI.id, SONNET.id, OPUS.id]
    data = plan.to_dict()
    assert 'second review family is anthropic' in data['reasons']
    assert [s['family'] for s in data['review_seats']] == ['gemini', 'claude']


def test_route_cannot_fill_two_families_names_reason(service, monkeypatch):
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (SONNET, OPUS))
    svc = service()
    with pytest.raises(routing.SpawnRefused, match='two distinct review families unavailable'):
        svc.route(CARD, 'review', ['gpt'], benchmark={})
    assert not svc.runner.calls


def test_alias_duplicate_seat_families_refused(service, monkeypatch):
    svc = service()
    plan = svc._route(CARD, 'review', ['mimo'], benchmark={})
    alias = replace(SONNET, id='anthropic-alias', family='anthropic')
    duplicate = replace(plan, legs=(SONNET, alias), review_seats=(SONNET, alias))
    monkeypatch.setattr(svc, '_route', lambda *a, **k: duplicate)
    with pytest.raises(routing.SpawnRefused, match='two distinct review families unavailable'):
        asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                              seats=True, wait_seconds=None, benchmark={}))
    assert not svc.runner.calls


def test_both_slots_skip_without_sharing_spare_family(service, monkeypatch):
    muse = leg('omniroute-muse-spark')
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (GEMINI, LUNA, SONNET, OPUS, muse))
    svc = service(skip=(GEMINI.model, LUNA.model))
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    assert [r['chosen_leg']['id'] for r in answer['seats']] == [SONNET.id, muse.id]
    for run, slot in zip(answer['seats'], answer['seat_coverage']):
        saved = json.loads(svc._path(run['run_id']).read_text())
        assert saved['plan']['review_seats'] == [run['chosen_leg']]
        assert slot['family'] == run['chosen_leg']['family']
    assert len({s['keeper_family'] for s in answer['seat_coverage']}) == 2
    group = json.loads((svc.state / 'seat-groups' / (answer['seat_group'] + '.json')).read_text())
    assert group['seat_coverage'] == answer['seat_coverage']


def test_second_slot_fallback_discloses_anthropic(service, monkeypatch):
    svc = service(skip=LUNA.model)
    plan = svc._route(CARD, 'review', ['mimo'], benchmark={})
    # First slot keeps a different spare, leaving Anthropic for the second slot.
    muse = leg('omniroute-muse-spark')
    plan = replace(plan, legs=(GEMINI, LUNA, muse, SONNET, OPUS))
    monkeypatch.setattr(svc, '_route', lambda *a, **k: plan)
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    second = answer['seats'][1]
    assert second['chosen_leg']['id'] == SONNET.id
    assert second['plan']['review_seats'] == [second['chosen_leg']]
    assert answer['seat_coverage'][1]['keeper_family'] == 'anthropic'
    assert 'second review family is anthropic' in answer['reasons']
