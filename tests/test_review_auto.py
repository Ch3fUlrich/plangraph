"""Automatic terminal-writer reviews: files and fake transports only."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner.runner import spawn_service

KEY, HEAD = 'demo:REVIEW', 'a' * 40


@pytest.fixture
def rig(tmp_path, monkeypatch, tools_importable):
    import seat_verdict
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    tokens = tmp_path / 'tokens'
    tokens.mkdir()
    monkeypatch.setenv('PG_REVIEWER_TOKEN_DIR', str(tokens))
    for family in ('google', 'xiaomi', 'meta'):
        (tokens / f'reviewer-{family}.token').write_text('fake')
    legs = tuple(next(l for l in DEFAULT_LEGS if l.family == f)
                 for f in ('gemini', 'mimo', 'muse'))
    item = dict(key=KEY, review_tier='standard', done_criteria='current done text',
                linked_decisions=['D-1'], evidence=[])
    keeper = SimpleNamespace(url='https://keeper.invalid', item=lambda k: item,
        decisions=lambda ids: ['| D-1 | decision row |'], deliver=lambda *a: None)
    service = spawn_service(SimpleNamespace(jobs={}, default_cwd=str(tmp_path)),
                            state=tmp_path, keeper_sink=keeper)
    calls, routes, posts, cancelled = [], [], [], []
    outcomes = [('completed', 'TEST_GAMING: no\nVERDICT: ACCEPT')]*3
    async def spawn(**kw):
        calls.append(kw)
        index = len(calls) - 1
        status, text = outcomes[min(index, len(outcomes)-1)]
        leg = kw['_plan'].first_leg
        head = kw['card']['diff_ref']['head']
        seat = dict(run_id='run-seat'+str(index), role='review', status=status,
                    **{k: kw['card'][k] for k in ('review_writer', 'diff_ref') if k in kw['card']},
                    item_key=KEY, ended=None if status == 'running' else 2,
                    exit_code=0 if status in ('completed', 'running') else 1,
                    given_writer_families=kw['writer_families'], attempts=[dict(stdout_tail=text)],
                    stdout_tail=text, served_model=leg.model, chosen_leg=dict(model=leg.model, family=leg.family),
                    completion=dict(head_sha=head, event_id='seat'+str(index)), finish_reason='stop')
        service._save(seat)
        if status == 'running':
            service.tasks[seat['run_id']] = asyncio.create_task(asyncio.Event().wait())
        return seat
    async def cancel(run_id):
        cancelled.append(run_id)
        service.tasks[run_id].cancel()
        await asyncio.gather(service.tasks[run_id], return_exceptions=True)
        seat = service.result(run_id)
        seat.update(status='cancelled', ended=3)
        service._save(seat)
        return seat
    def route(*args, **kwargs):
        routes.append(args)
        return SpawnPlan(legs, (), 'test', (), (), 'two distinct review families unavailable')
    monkeypatch.setattr(service, 'spawn', spawn)
    monkeypatch.setattr(service, 'cancel', cancel)
    monkeypatch.setattr(service, '_route', route)
    monkeypatch.setattr('plangraph.keeper.evidence_ci.CIAdapter.check_runs',
                        lambda *a: [dict(name='ci', conclusion='success')])
    monkeypatch.setattr(seat_verdict, 'send', lambda url, token, payload: (
        posts.append(payload) or (200, '{}')))
    writer = dict(run_id='run-writer', role='implement', item_key=KEY, status='completed',
                  exit_code=0, ended=1, writer_family='gpt', cwd=str(tmp_path),
                  completion=dict(head_sha=HEAD, event_id='writer'))
    monkeypatch.setattr(service, 'completion_sink', SimpleNamespace(deliver=lambda *a: None))
    return SimpleNamespace(**locals())


async def drain(service):
    while pending := [t for k, t in service.tasks.items() if k.startswith('review-') and not t.done()]:
        await asyncio.gather(*pending)


@pytest.mark.parametrize('tier,need', [('standard', 1), ('high', 2)])
def test_done_writer_automatically_spawns_tier_seats(rig, tier, need):
    rig.item['review_tier'] = tier
    async def run():
        rig.service._save(rig.writer)
        await drain(rig.service)
    asyncio.run(run())
    assert len(rig.calls) == need
    assert len(rig.routes) == 1
    assert len(rig.service.result('run-writer')['review_runs']) == need
    assert len(rig.posts) == need
    assert 'current done text' in rig.calls[0]['prompt']
    assert 'decision row' in rig.calls[0]['prompt']
    assert all(c['writer_families'] == ['openai'] for c in rig.calls)


@pytest.mark.parametrize('status,text', [('completed', ''), ('failed', ''), ('completed', 'No verdict')])
def test_empty_failed_or_verdictless_seat_uses_next_leg(rig, status, text):
    rig.outcomes[0] = status, text
    async def run():
        rig.service._save(rig.writer)
        await drain(rig.service)
    asyncio.run(run())
    assert [c['_plan'].first_leg for c in rig.calls] == list(rig.legs[:2])
    assert len(rig.posts) == 1


def test_changes_stops_and_reports_findings_without_session(rig):
    rig.item['review_tier'] = 'high'
    rig.outcomes[0] = 'completed', 'HIGH: broken boundary\nVERDICT: CHANGES'
    async def run():
        rig.service._save(rig.writer)
        await drain(rig.service)
    asyncio.run(run())
    assert len(rig.calls) == 1
    assert 'broken boundary' in str(rig.service.result('run-writer')['review_findings'])
    assert rig.posts[0]['ok'] is False


def test_moved_head_voids_running_seat_and_reviews_fix_delta(rig):
    rig.outcomes[0] = 'running', ''
    rig.item['review_tier'] = 'high'
    async def run():
        rig.service._save(rig.writer)
        async with asyncio.timeout(2):
            while not rig.calls:
                await asyncio.sleep(0)
        # The old seat has been persisted before a new completion arrives.
        await asyncio.sleep(0)
        writer = rig.service.result('run-writer')
        writer['completion']['head_sha'] = 'b'*40
        rig.service._save(writer)
        await drain(rig.service)
    asyncio.run(run())
    assert rig.cancelled == ['run-seat0']
    assert len(rig.calls) == 2
    assert rig.calls[1]['card']['diff_ref'] == dict(repo=str(rig.tmp_path), base=HEAD, head='b'*40)


def test_exhaustion_and_red_checks_fail_closed(rig, monkeypatch):
    rig.outcomes[:] = [('failed', '')]*3
    async def run():
        rig.service._save(rig.writer)
        await drain(rig.service)
        assert len(rig.calls) == len(rig.legs)
        assert rig.service.result('run-writer')['review_auto_status'] == 'exhausted'
        monkeypatch.setattr('plangraph.keeper.evidence_ci.CIAdapter.check_runs', lambda *a: [])
        writer = rig.service.result('run-writer')
        writer['completion']['head_sha'] = 'c'*40
        rig.service._save(writer)
        await drain(rig.service)
        assert len(rig.calls) == len(rig.legs)
    asyncio.run(run())


def test_superseded_automatic_seat_cannot_post(rig):
    from plangraph.spawner.service import SpawnService
    SpawnService._save(rig.service, dict(rig.writer, review_head='b'*40, review_auto_status='running',
                                        review_checks=dict(head_sha='b'*40, green=True)))
    async def run():
        await rig.spawn(card=dict(diff_ref=dict(head=HEAD)), prompt='', role='review',
                        writer_families=['openai'], _plan=SpawnPlan(rig.legs, (), 'test', (), ()))
    asyncio.run(run())
    record = rig.service.result('run-seat0')
    record['review_writer'] = 'run-writer'
    directory = rig.service._path('run-seat0').parent
    (directory / 'verdict.delivered').unlink(missing_ok=True)
    (directory / 'verdict.json').unlink(missing_ok=True)
    rig.posts.clear()
    rig.service._save(record)
    assert not rig.posts
    assert json.loads((directory / 'verdict.json').read_text())['reason'] == 'superseded or unverified automatic review'


def test_automatic_seat_keeps_record_only_posting_evidence(tmp_path):
    from plangraph.spawner.runner import JobInfo
    from plangraph.spawner.seat_artifact import publish
    job = JobInfo('job-seat', 'completed', '', 'gemini', exit_code=0,
                  served_model='gemini-3.8-flash', stdout='TEST_GAMING: no\nVERDICT: ACCEPT')
    job.response_finish_reason, job.prompt_sha256 = 'stop', 'a'*64
    publish(tmp_path, dict(role='review', ended=1, status='completed', exit_code=0,
                          run_id='run-seat', review_writer='run-writer'), job)
    assert not (tmp_path / 'evidence.json').exists()
