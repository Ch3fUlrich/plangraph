"""Automatic terminal-writer reviews: files and fake transports only."""
import asyncio
import inspect
import io
import json
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.service import SpawnService
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
        from plangraph.spawner.item_key import annotate_item
        calls.append(kw)
        index = len(calls) - 1
        status, text = outcomes[min(index, len(outcomes)-1)]
        leg = kw['_plan'].first_leg
        head = kw['card']['diff_ref']['head']
        seat = dict(run_id='run-seat'+str(index), role='review', status=status,
                    item_key=KEY, ended=None if status == 'running' else 2,
                    exit_code=0 if status in ('completed', 'running') else 1,
                    given_writer_families=kw['writer_families'], attempts=[dict(stdout_tail=text)],
                    stdout_tail=text, served_model=leg.model, chosen_leg=dict(model=leg.model, family=leg.family),
                    completion=dict(head_sha=head, event_id='seat'+str(index)), finish_reason='stop')
        annotate_item(seat, {'item_key': KEY, **kw['card']}, review_writer=kw.get('_review_writer'))
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
        for method in (SpawnService._route, type(service)._route):
            inspect.signature(method).bind(service, *args, **kwargs)
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


@pytest.mark.parametrize('checks', [[], [dict(name='ci', conclusion='failure')],
    [dict(name='ci', status='in_progress', conclusion=None)], [dict(name='ci', conclusion='skipped')]])
def test_exhaustion_and_red_checks_fail_closed(rig, monkeypatch, checks):
    rig.outcomes[:] = [('failed', '')]*3
    async def run():
        rig.service._save(rig.writer)
        await drain(rig.service)
        assert len(rig.calls) == len(rig.legs)
        assert rig.service.result('run-writer')['review_auto_status'] == 'exhausted'
        monkeypatch.setattr('plangraph.keeper.evidence_ci.CIAdapter.check_runs', lambda *a: checks)
        writer = rig.service.result('run-writer')
        writer['completion']['head_sha'] = 'c'*40
        rig.service._save(writer)
        assert 'review-run-writer-' + 'c'*40 in rig.service.tasks
        await drain(rig.service)
        assert len(rig.calls) == len(rig.legs)
        assert rig.service.result('run-writer')['review_auto_status'] == 'blocked'
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


@pytest.mark.parametrize('inflight,head', [(False, HEAD), (False, 'b'*40), (True, HEAD)])
def test_new_high_writer_needs_two_families_and_cancels_superseded_seats(rig, inflight, head):
    rig.item['review_tier'] = 'high'
    if inflight:
        rig.outcomes[0] = 'running', ''
    async def run():
        rig.service._save(rig.writer)
        if inflight:
            async with asyncio.timeout(2):
                while not rig.calls:
                    await asyncio.sleep(0)
        else:
            await drain(rig.service)
        before = len(rig.calls)
        rig.service._save(dict(rig.writer, run_id='run-replacement', started=5,
                              completion=dict(head_sha=head, event_id='replacement')))
        await drain(rig.service)
        assert len(rig.calls) - before == 2
        assert rig.service.result('run-writer')['review_auto_status'] == 'voided'
        assert rig.service.result('run-replacement')['review_auto_status'] == 'accepted'
        assert rig.cancelled == (['run-seat0'] if inflight else [])
    asyncio.run(run())


def test_resaved_voided_writer_does_not_interrupt_replacement(rig):
    rig.outcomes[0] = 'running', ''
    async def run():
        SpawnService._save(rig.service, dict(rig.writer, run_id='run-old',
            review_head=HEAD, review_auto_status='voided'))
        rig.service._save(rig.writer)
        async with asyncio.timeout(2):
            while not rig.calls:
                await asyncio.sleep(0)
        rig.service._save(rig.service.result('run-old'))
        assert not rig.service._review_changed['run-writer', HEAD].is_set()
    asyncio.run(run())


def test_third_push_while_voided_queues_latest_head(rig, monkeypatch):
    rig.outcomes[0] = 'running', ''
    cancel = rig.cancel
    async def push_during_cancel(run_id):
        result = await cancel(run_id)
        writer = rig.service.result('run-writer')
        assert writer['review_auto_status'] == 'voided'
        writer['completion']['head_sha'] = 'c'*40
        rig.service._save(writer)
        assert 'review-run-writer-' + 'c'*40 in rig.service.tasks
        return result
    monkeypatch.setattr(rig.service, 'cancel', push_during_cancel)
    async def run():
        rig.service._save(rig.writer)
        async with asyncio.timeout(2):
            while not rig.calls:
                await asyncio.sleep(0)
        writer = rig.service.result('run-writer')
        writer['completion']['head_sha'] = 'b'*40
        rig.service._save(writer)
        await drain(rig.service)
    asyncio.run(run())
    writer = rig.service.result('run-writer')
    assert writer['completion']['head_sha'] == writer['review_head'] == 'c'*40
    assert writer['review_auto_status'] == 'accepted'
    assert [c['card']['diff_ref']['head'] for c in rig.calls] == [HEAD, 'c'*40]


def test_completion_head_is_reread_after_authority_awaits(rig, monkeypatch):
    def route(*args):
        writer = rig.service.result('run-writer')
        writer['completion']['head_sha'] = 'b'*40
        SpawnService._save(rig.service, writer)
        return rig.route(*args)
    monkeypatch.setattr(rig.service, '_route', route)
    asyncio.run(_saved_and_drained(rig))
    assert not rig.calls
    assert rig.service.result('run-writer')['completion']['head_sha'] == 'b'*40


async def _saved_and_drained(rig):
    rig.service._save(rig.writer)
    await drain(rig.service)

def test_driver_route_binds_real_service_methods(rig):
    asyncio.run(_saved_and_drained(rig))
    assert len(rig.routes) == 1
    assert rig.service.result('run-writer')['review_auto_status'] == 'accepted'

def test_missing_decision_field_blocks_closed(rig):
    del rig.item['linked_decisions']
    asyncio.run(_saved_and_drained(rig))
    assert not rig.calls
    assert rig.service.result('run-writer')['review_auto_status'] == 'blocked'


def test_blocked_head_can_retry_after_transport_recovers(rig, monkeypatch):
    monkeypatch.delattr(rig.keeper, 'url')
    asyncio.run(_saved_and_drained(rig))
    assert rig.service.result('run-writer')['review_auto_status'] == 'blocked'
    rig.keeper.url = 'https://keeper.invalid'
    asyncio.run(_saved_and_drained(rig))
    assert rig.service.result('run-writer')['review_auto_status'] == 'accepted'


def test_manual_card_cannot_select_automatic_authority(rig, monkeypatch):
    from plangraph.spawner.item_key import annotate_item
    from plangraph.spawner.notify import completion_event
    SpawnService._save(rig.service, dict(rig.writer, review_head=HEAD, review_auto_status='running',
        review_checks=dict(head_sha=HEAD, green=True)))
    card = dict(item_key=KEY, review_writer='run-writer', diff_ref=dict(head=HEAD))
    record = dict(run_id='run-manual', role='review', status='completed', exit_code=0)
    annotate_item(record, card)
    assert 'review_writer' not in record
    monkeypatch.setattr('plangraph.spawner.notify.subprocess.check_output', lambda *a, **kw: 'b'*40)
    assert completion_event(record, rig.tmp_path)['head_sha'] == 'b'*40
    asyncio.run(rig.spawn(card=card, _plan=SpawnPlan(rig.legs, (), 'test', (), ()), writer_families=['openai']))
    assert not rig.posts


@pytest.mark.parametrize('dispatch', [False, True])
@pytest.mark.parametrize('automatic', [False, True])
def test_real_spawn_persists_only_driver_supplied_review_writer(rig, monkeypatch, dispatch, automatic):
    from registry_route_util import _service
    from plangraph.spawner import seat_spawn
    service = _service(rig.tmp_path, monkeypatch)
    if dispatch:
        service = spawn_service(service.runner, state=rig.tmp_path, keeper_sink=rig.keeper)
    monkeypatch.setattr(seat_spawn, 'admit', lambda *a, **kw: None)
    async def prepare(prompt, *args, **kwargs):
        return prompt
    monkeypatch.setattr(service, '_prepare_leg_prompt', prepare)
    result = asyncio.run(service.spawn(card=dict(estimate_tokens=100, item_key=KEY,
        review_writer='run-forged', diff_ref=dict(repo=str(rig.tmp_path), base='origin/main', head=HEAD)),
        role='review', writer_families=['openai'],
        dry_run=True, wait_seconds=None, _review_writer='run-writer' if automatic else None))
    assert result.get('review_writer') == ('run-writer' if automatic else None)


def test_interim_python_exception_cites_area_unblock_authority():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    entry, = [line for line in (root / 'tools/new_code_allowlist.txt').read_text().splitlines()
              if line.startswith('src/plangraph/spawner/review_auto.py |')]
    assert 'D-5083 rule 9.14' in entry and 'retired by SPAWNER-RUST-PORT' in entry

@pytest.mark.parametrize('owner', ['unrelated', 'seat', 'review', 'group', 'automatic', 'voided'])
@pytest.mark.parametrize('cancelled', [True, False])
def test_supervisor_ignores_only_unrelated_task_failures(rig, monkeypatch, owner, cancelled):
    from plangraph.spawner import cli
    async def fail():
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError('task failed')
    async def spawn(**kwargs):
        key = {'unrelated': 'run-other', 'seat': 'run-writer',
               'review': 'review-run-writer-' + HEAD, 'group': 'run-child',
               'automatic': 'run-auto', 'voided': 'run-old'}[owner]
        rig.service.tasks[key] = asyncio.create_task(fail())
        SpawnService._save(rig.service, dict(rig.writer,
            review_runs=[key] if owner in ('automatic', 'voided') else [],
            review_voided=[key] if owner == 'voided' else []))
        if owner == 'group':
            SpawnService._save(rig.service, dict(rig.writer, run_id='run-child'))
        return dict(seats=[dict(run_id='run-child')]) if owner == 'group' else rig.writer
    monkeypatch.setattr(rig.service, 'spawn', spawn)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: rig.service)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO('{"state":"unused"}\n'))
    monkeypatch.setattr(cli.sys, 'stdout', io.StringIO())
    if owner in ('unrelated', 'voided'):
        assert cli._supervise() == 0
    else:
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            cli._supervise()
