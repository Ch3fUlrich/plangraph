"""D-5520: seat slots describe the resolved leg, not their initial family."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
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


@pytest.mark.parametrize('reason', ['unknown', 'UNKNOWN', ' unknown ', None, ''])
def test_unknown_adapter_skip_has_named_reason(service, monkeypatch, reason):
    svc = service()
    plan = svc._route(CARD, 'review', ['mimo'], benchmark={})
    if reason != 'unknown':
        monkeypatch.setattr(svc, '_route', lambda *a, **k: plan)
    svc.runner.leg_reader.leg_status = lambda model, **k: (
        dict(state='unknown' if not reason else 'limited', reason=reason, evidence=None)
        if model in (GEMINI.model, 'agy/' + GEMINI.model)
        else dict(state='ok', evidence=None))
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    run = next(r for r in answer['seats'] if any(
        isinstance(a['leg'], dict) and a['leg']['id'] == GEMINI.id for a in r['attempts']))
    saved = json.loads(svc._path(run['run_id']).read_text())
    for record in (run, saved):
        assert all(a.get('skip_reason') and a['skip_reason'].strip().lower() != 'unknown'
                   for a in record['attempts'] if a['status'] == 'skipped')
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
    docs = (Path(__file__).resolve().parents[1] / 'docs/spawner-seats.md').read_text()
    assert '`route.review_seats` names planned slots; their family is final only after dispatch.' in docs


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


@pytest.mark.parametrize('dispatch', [False, True], ids=['base', 'dispatch'])
def test_both_slots_skip_without_sharing_spare_family(service, monkeypatch, dispatch):
    muse = leg('omniroute-muse-spark')
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (GEMINI, LUNA, SONNET, OPUS, muse))
    svc = service(dispatch, skip=(GEMINI.model, LUNA.model))
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


@pytest.mark.parametrize('dispatch', [False, True], ids=['base', 'dispatch'])
def test_resolved_duplicate_family_leaves_second_slot_unfilled(service, monkeypatch, dispatch):
    svc = service(dispatch)
    spawn = svc._spawn

    async def duplicate(**kwargs):
        record = await spawn(**kwargs)
        if kwargs.get('_plan') is not None:
            from dataclasses import asdict
            record['chosen_leg'] = asdict(SONNET)
            svc._save(record)
        return record

    monkeypatch.setattr(svc, '_spawn', duplicate)
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    filled = [r for r in answer['seats'] if r.get('chosen_leg')]
    assert len(filled) == 1
    unfilled, = [r for r in answer['seats'] if not r.get('chosen_leg')]
    saved = json.loads(svc._path(unfilled['run_id']).read_text())
    for record in (unfilled, saved):
        assert record['plan']['review_seats'] == []
        slot, = record['plan']['seat_coverage']
        assert slot['family'] is slot['keeper_family'] is None
        assert 'distinct_family_unavailable' in slot['reason']
        assert 'unknown' not in slot['reason']
    assert len({s['keeper_family'] for s in answer['seat_coverage'] if s['family']}) == 1


def test_record_reasons_refuse_alias_duplicates_and_never_normalize_none(monkeypatch):
    from plangraph.planner import seats
    from plangraph.spawner.seat_artifact import seat_reasons
    normalize = seats.seat_family

    def nonnull(family):
        assert family is not None, 'unfilled slots have no family to normalize'
        return normalize(family)

    monkeypatch.setattr(seats, 'seat_family', nonnull)
    assert seat_reasons(['gemini', None], ['distinct_family_unavailable']) == ('distinct_family_unavailable',)
    with pytest.raises(routing.SpawnRefused, match='duplicate resolved seat family'):
        seat_reasons(['claude', 'anthropic'], [])


def test_record_reasons_reject_resolved_alias_duplicates():
    from plangraph.spawner.seat_artifact import seat_reasons
    with pytest.raises(routing.SpawnRefused, match='duplicate resolved seat family'):
        seat_reasons(['claude', 'anthropic'], [])


def test_reroute_updates_slot_before_fallback_launch_or_save(service, monkeypatch):
    svc = service(skip=GEMINI.model)
    spawn = svc.runner.spawn
    seen = []

    async def inspect_plan(**kwargs):
        if kwargs['model_or_combo'] == SONNET.model:
            record = next(r for r in svc._running_records.values() if any(
                a.get('skip_reason') == 'provider_stop' for a in r['attempts']))
            seen.append(json.loads(json.dumps(record['plan'])))
        return await spawn(**kwargs)

    monkeypatch.setattr(svc.runner, 'spawn', inspect_plan)
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    assert len(seen) == 1
    assert [s['id'] for s in seen[0]['review_seats']] == [SONNET.id]
    slot, = seen[0]['seat_coverage']
    assert (slot['leg'], slot['family']) == (SONNET.id, SONNET.family)
    assert all(r['status'] == 'completed' for r in answer['seats'])


@pytest.mark.parametrize('dispatch', [False, True], ids=['base', 'dispatch'])
def test_same_family_spares_are_owned_by_one_slot_with_alternates(service, monkeypatch, dispatch):
    muse = leg('omniroute-muse-spark')
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (GEMINI, LUNA, SONNET, OPUS, muse))
    svc = service(dispatch, skip=(GEMINI.model, LUNA.model, SONNET.model))
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    assert [r['chosen_leg']['id'] for r in answer['seats']] == [OPUS.id, muse.id]
    assert [s['keeper_family'] for s in answer['seat_coverage']] == ['anthropic', 'meta']
    for record in answer['seats']:
        saved = json.loads(svc._path(record['run_id']).read_text())
        assert saved['plan']['review_seats'] == [record['chosen_leg']]


@pytest.mark.parametrize('dispatch', [False, True], ids=['base', 'dispatch'])
def test_same_family_spares_cannot_fill_both_slots(service, monkeypatch, dispatch):
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (GEMINI, LUNA, SONNET, OPUS))
    svc = service(dispatch, skip=(GEMINI.model, LUNA.model))
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    assert [r.get('chosen_leg', {}).get('id') if r.get('chosen_leg') else None
            for r in answer['seats']] == [SONNET.id, None]
    assert answer['seat_coverage'][1]['family'] is None
    assert 'distinct_family_unavailable' in answer['seat_coverage'][1]['reason']
    assert 'provider_stop' in answer['seat_coverage'][1]['reason']


@pytest.mark.parametrize('reason', ['unknown', 'UNKNOWN', ' unknown ', None, ''])
@pytest.mark.parametrize('probe_state,expected', [
    (None, 'down: leg_probe_failed'), ('host-not-ready', 'host_not_ready'),
    ('probe_failed_leg_serving', None)])
def test_probe_skip_paths_never_return_unknown(monkeypatch, probe_state, expected, reason):
    from plangraph.spawner import leg_probe
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1')
    state = SimpleNamespace(leg_status=lambda *a, **k: dict(state='ok'),
                            last_probe_ts=lambda *a: None, record_probe_fail=lambda *a: None)

    async def probe(*a, **k):
        return dict(ok=False, reason=reason, state=probe_state)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    runner = SimpleNamespace(leg_reader=state)
    assert asyncio.run(leg_probe.before_launch(runner, GEMINI, 'implement', False)) == expected


@pytest.mark.parametrize('families', [(SONNET.family, 'anthropic'), (SONNET.family, SONNET.family)])
def test_group_record_duplicate_guard_persists_unfilled_slot(service, families):
    from dataclasses import asdict
    from plangraph.spawner.seat_artifact import sync_seat_group
    svc = service(False)
    records = [dict(run_id='run-fixture' + str(i), role='review', status='completed',
                    exit_code=0, ended=1, chosen_leg=dict(asdict(SONNET), family=family),
                    plan=dict(seat_coverage=[dict(seat_group='seats-fixture')]))
               for i, family in enumerate(families)]
    sync_seat_group(records, svc._save)
    assert records[0]['chosen_leg']
    assert records[1]['chosen_leg'] is None
    saved = svc.result(records[1]['run_id'])
    assert saved['plan']['review_seats'] == []
    assert saved['plan']['seat_coverage'][0]['family'] is None
    assert 'distinct_family_unavailable' in saved['plan']['seat_coverage'][0]['reason']


def test_spare_writer_family_is_never_launched(service, monkeypatch):
    svc = service(skip=(GEMINI.model, LUNA.model))
    plan = svc._route(CARD, 'review', ['mimo'], benchmark={})
    writer = leg('mimo-v2.6-flash')
    monkeypatch.setattr(svc, '_route', lambda *a, **k: replace(plan, legs=(GEMINI, LUNA, writer)))
    answer = asyncio.run(svc.spawn(card=CARD, role='review', writer_families=['mimo'],
                                   seats=True, wait_seconds=None, benchmark={}))
    assert all(not r['chosen_leg'] for r in answer['seats'])
    assert writer.model not in [call['model_or_combo'] for call in svc.runner.calls]
    assert all(s['family'] is None and 'distinct_family_unavailable' in s['reason']
               for s in answer['seat_coverage'])


@pytest.mark.parametrize('coverage,seat', [
    ((), False), (None, False), ((None, 'invalid-slot'), False),
    ((dict(leg=LUNA.id, seat_group='seats-fixture'),), True),
    ((None, 'invalid-slot', dict(leg=LUNA.id, seat_group='seats-fixture')), True)])
def test_stopped_seat_keeps_status_and_carries_provider_stop(service, coverage, seat):
    svc = service(False, skip=(GEMINI.model, LUNA.model))
    plan = routing.SpawnPlan((GEMINI, LUNA), (), 'test', (LUNA,), (), seat_coverage=coverage)
    answer = asyncio.run(svc._spawn(card=CARD, role='review', wait_seconds=None, _plan=plan))
    saved = json.loads(svc._path(answer['run_id']).read_text())
    for record in (answer, saved):
        nonseat, stopped = record['attempts']
        assert nonseat['status'] == 'completed'
        assert nonseat['skip_reason'] is None
        assert stopped['status'] == record['status'] == 'completed'
        assert stopped['skip_reason'] == ('provider_stop' if seat else None)
        assert record['exit_code'] == 0
        if seat:
            assert 'provider_stop' in record['error']
            assert 'distinct_family_unavailable' in record['error']
        else:
            assert not record.get('error')
    assert all(job.status == 'completed' and job.exit_code == 0 for job in svc.runner.jobs.values())


@pytest.mark.parametrize('dispatch', [False, True], ids=['base', 'dispatch'])
def test_stopped_seat_is_failed_without_labeling_nonseat_legs(service, dispatch):
    # A genuinely failed stopped job fails; completed jobs above stay completed (D-5657).
    svc = service(dispatch)
    spawn = svc.runner.spawn

    async def failed_stop(**kwargs):
        job = await spawn(**kwargs)
        job.status, job.exit_code, job.stderr = 'failed', 7, ''
        svc.runner.provider_responses[job.job_id] = {'status': 429, 'body': 'rate limited'}
        return job

    svc.runner.spawn = failed_stop
    plan = routing.SpawnPlan((LUNA,), (), 'test', (LUNA,), (),
                            seat_coverage=(dict(leg=LUNA.id, seat_group='seats-fixture'),))
    answer = asyncio.run(svc._spawn(card=CARD, role='review', wait_seconds=None, _plan=plan))
    assert answer['status'] == 'failed' and answer['exit_code'] != 0
    assert answer['attempts'][0]['skip_reason'] == 'provider_stop'


@pytest.mark.parametrize('nonseat', [None, 'invalid-slot', dict(leg=GEMINI.id)])
def test_mixed_coverage_cannot_label_an_unrelated_leg(nonseat):
    from dataclasses import asdict
    from plangraph.spawner.seat_artifact import sync_seat
    coverage = [dict(leg=LUNA.id, seat_group='seats-fixture'), nonseat]
    record = dict(role='review', chosen_leg=asdict(GEMINI), status='completed', ended=1,
                  plan=dict(seat_coverage=coverage, review_seats=[asdict(LUNA)]))
    sync_seat(record)
    assert coverage[0] == dict(leg=LUNA.id, seat_group='seats-fixture')
    assert record['plan']['review_seats'] == [asdict(LUNA)]
    assert record['chosen_leg']['id'] == GEMINI.id
