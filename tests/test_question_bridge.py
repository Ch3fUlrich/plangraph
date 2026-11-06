"""Hermetic question exits: fake captured output, no harness or gateway."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from plangraph.spawner.question_bridge import answer_prompt, question_exit, rounds_exhausted
from plangraph.spawner.exit_outcome import fallthrough_reason, terminal_reason
from plangraph.spawner.runner import spawn_service


PACKET = 'PG_QUESTION: {"text":"Which database?"}'


def job(**kw):
    return NS(**dict(dict(status='completed', role='implement', stdout=PACKET,
                         stderr='', exit_code=0, events=[], survivors=[], final_message=kw.get('stdout', PACKET)), **kw))


def test_final_only_and_last_line_rule():
    j = job(stdout=PACKET, final_message='Done')
    assert not question_exit(j, {})
    j = job(stdout=json.dumps({'type': 'tool_result', 'content': PACKET}) + '\n' +
            json.dumps({'type': 'result', 'result': 'Done'}), final_message=None)
    assert not question_exit(j, {})
    assert not question_exit(job(stdout=json.dumps({'type': 'tool_result', 'content': PACKET}), final_message=None), {})


def test_combined_output_last_line_question():
    j = job(stdout='progress\n' + PACKET, final_message=None)
    assert question_exit(j, {}) and j.question['text'] == 'Which database?'
    assert not question_exit(job(stdout=PACKET + '\nDone', final_message=None), {})
    j = job(stdout=PACKET + '\nPG_QUESTION: nope', final_message=None)
    assert not question_exit(j, {})
    assert j.events == [{'event': 'question_packet_malformed'}]


@pytest.mark.parametrize('streamed', [False, True])
def test_final_message_last_valid_packet(streamed):
    text = PACKET + '\nPG_QUESTION: {"text":"new"}\nPG_QUESTION: nope\nDone'
    j = (job(stdout=json.dumps({'type': 'result', 'result': text}), final_message=None)
         if streamed else job(final_message=text))
    assert question_exit(j, {}) and j.question['text'] == 'new'
    assert j.events == [{'event': 'question_packet_malformed'}]


@pytest.mark.parametrize('packet', ['nope', '{}', '{"text":" "}',
    '{"text":1}', '{"text":"q","default":2}',
    '{"text":"q","deadline_s":true}', '{"text":"q","deadline_s":-1}',
    '{"text":"q","risk_tags":null}', '{"text":"q","risk_tags":[1]}'])
def test_malformed_named_event(packet):
    j = job(stdout='PG_QUESTION: ' + packet)
    assert not question_exit(j, {})
    assert j.status == 'completed'
    assert j.events == [{'event': 'question_packet_malformed'}]


def test_packet_shape_and_answer():
    q = dict(text='q', default=None, deadline_s=None, risk_tags=['auth'], extra=1)
    j = job(stdout='PG_QUESTION: ' + json.dumps(q))
    assert question_exit(j, {})
    assert j.question == {k: v for k, v in q.items() if k != 'extra'}
    assert answer_prompt('task', 'yes') == 'task\n\nAnswer to your question: yes'


@pytest.mark.parametrize('reason', ['git_config_mutated', 'cancelled', 'kill_incomplete'])
def test_protected(reason):
    r = dict(reason=reason, status='failed', exit_code=1)
    j = job()
    assert not question_exit(j, r)
    terminal_reason(r, j)
    assert r['reason'] == reason and r['status'] == 'failed'


@pytest.mark.parametrize('source', ['record', 'job'])
def test_model_refusal_reason_skips_question_and_fallthrough(source):
    j, r = job(status='failed', exit_code=1), dict(status='failed', exit_code=1)
    if source == 'record':
        r['reason'] = 'model_refusal'
    else:
        j.reason = 'model_refusal'
    assert not question_exit(j, r)
    fallthrough_reason(None, None, j, r)
    terminal_reason(r, j)
    assert j.status == r['status'] == 'failed'
    assert (r['reason'] if source == 'record' else j.reason) == 'model_refusal'


@pytest.mark.parametrize('status', ['cancelled', 'timeout', 'stalled'])
def test_killed(status):
    assert not question_exit(job(status=status), {})


@pytest.mark.parametrize('kw', [dict(reason='kill_incomplete'), dict(events=[{'event': 'kill_incomplete'}])])
def test_job_kill_incomplete(kw):
    j = job(**kw)
    assert not question_exit(j, {}) and j.status == 'completed'


def test_validation_optimized():
    import subprocess
    import sys
    code = '''import sys
sys.path.insert(0, 'src')
from types import SimpleNamespace as NS
from plangraph.spawner.question_bridge import question_exit
for packet in ('{"text":1}', '{"text":"q","deadline_s":true}', '{"text":"q","risk_tags":null}'):
    j = NS(status='completed', role='implement', events=[], final_message='PG_QUESTION: ' + packet)
    if question_exit(j, {}) or j.events != [{'event': 'question_packet_malformed'}]:
        raise RuntimeError('invalid packet accepted')
'''
    subprocess.run([sys.executable, '-O', '-c', code], check=True)


@pytest.mark.parametrize('role,text', [('review', PACKET), ('implement', 'Done')])
def test_unchanged(role, text):
    j, r = job(role=role, stdout=text), {}
    before = vars(j).copy()
    assert not question_exit(j, r)
    assert r == {} and j.status == before['status']
    assert j.stdout == before['stdout'] and j.events == []


def test_fallthrough_and_reason():
    j = job(stderr='error: quota reached', exit_code=7, status='failed')
    r = {}
    assert fallthrough_reason(None, None, j, r) is None
    terminal_reason(r, j)
    assert r['status'] == 'needs_input' and r['exit_code'] == 0


def test_rounds_item_then_hash(monkeypatch):
    import hashlib
    card = {'item': 'plangraph:x'}
    rows = [dict(status='needs_input', item_key='plangraph:x', card_hash='other')] * 3
    service = NS(ps=lambda: {'runs': rows})
    assert rounds_exhausted(service, card, 'implement')
    assert not rounds_exhausted(service, {'item': 'plangraph:y'}, 'implement')
    assert not rounds_exhausted(service, card, 'review')
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
    card = {'title': 'x'}
    digest = hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()
    rows[:] = [dict(status='needs_input', card_hash=digest)] * 3
    assert rounds_exhausted(service, card, 'implement')
    monkeypatch.setenv('PLANGRAPH_MAX_QUESTION_ROUNDS', '4')
    assert not rounds_exhausted(service, card, 'implement')


@pytest.mark.parametrize('card', [None, {}])
def test_rounds_without_card(card, monkeypatch):
    def forbidden(*a):
        pytest.fail('cardless run attempted item annotation')
    monkeypatch.setattr('plangraph.spawner.item_key.annotate_item', forbidden)
    assert not rounds_exhausted(NS(ps=lambda: {'runs': []}), card, 'implement')


@pytest.mark.parametrize('override', [dict(seats=True), dict(model='test'), dict(leg='test')])
def test_spawn_without_card_delegates(tmp_path, monkeypatch, override):
    service = spawn_service(NS(), tmp_path / 'state')
    calls = []
    async def base_spawn(self, **kw):
        calls.append(kw)
        return {'status': 'delegated'}
    monkeypatch.setattr(type(service).__mro__[1], '_spawn', base_spawn)
    def forbidden(*a):
        pytest.fail('cardless override attempted item annotation')
    monkeypatch.setattr('plangraph.spawner.item_key.annotate_item', forbidden)
    assert asyncio.run(service._spawn(card=None, **override)) == {'status': 'delegated'}
    assert len(calls) == 1 and calls[0]['card'] is None
    assert all(calls[0][key] == value for key, value in override.items())


def test_dispatch_wait_event_and_no_continuation(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    calls, delivered = [], []
    runner = NS(_processes={}, _tasks={}, provider_responses={}, jobs={})
    async def spawn(**kw):
        calls.append(kw)
        j = job(job_id='fake', cwd=str(tmp_path), served_model=None, served_family=None,
                served_witness_reason=None, request_side_model=None, witness_kind=None)
        runner.jobs[j.job_id] = j
        return j
    runner.spawn = spawn
    sink = NS(deliver=lambda recipient, event: delivered.append(event))
    service = spawn_service(runner, tmp_path / 'state', completion_sink=sink, keeper_sink=sink)
    plan = SpawnPlan(tuple(DEFAULT_LEGS[:2]), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'task')
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    async def no_probe(*a):
        return None
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', no_probe)
    async def forbidden(*a):
        pytest.fail('question spent a fix/continuation attempt')
    monkeypatch.setattr(service, '_continue_writer', forbidden)
    async def run():
        r = await service._spawn(card={'title': 'x', 'item': 'plangraph:x'}, wait_seconds=None)
        assert (await service.wait(r['run_id'], timeout=0))['status'] == 'needs_input'
        return r
    r = asyncio.run(run())
    assert r['status'] == 'needs_input' and len(calls) == 1
    assert r['attempts'][0]['status'] == 'needs_input'
    assert r['completion']['event'] == 'NEEDS_INPUT'
    assert all(e['question']['text'] == 'Which database?' for e in delivered)
    row = service.ps()['runs'][0]
    assert row['card_hash'] == r['card_hash'] and row['status'] == 'needs_input'
    assert row['item_key'] == r['item_key'] == 'plangraph:x'
    monkeypatch.setenv('PLANGRAPH_MAX_QUESTION_ROUNDS', 'x')
    assert asyncio.run(service._spawn(card={'title': 'x', 'item': 'plangraph:x'}, wait_seconds=None))['status'] == 'needs_input'
    monkeypatch.setenv('PLANGRAPH_MAX_QUESTION_ROUNDS', '1')
    from plangraph.routing_errors import SpawnRefused
    with pytest.raises(SpawnRefused, match='question_rounds_exhausted'):
        asyncio.run(service._spawn(card={'title': 'changed', 'item': 'plangraph:x'}, wait_seconds=None))
    assert len(calls) == 2


def test_monitor_skips_unchanged_writer(tmp_path, monkeypatch):
    from test_claude_stream_render import fake_claude
    from plangraph.spawner.runner import SpawnerRunner
    async def run():
        runner = SpawnerRunner(process_factory=fake_claude(tmp_path, [
            json.dumps({'type': 'result', 'result': PACKET})]), default_cwd=str(tmp_path))
        from plangraph.planner.legs import DEFAULT_LEGS
        model = next(l.model for l in DEFAULT_LEGS if l.harness == 'claude')
        j = await runner.spawn(prompt='task', model_or_combo=model, client='claude', role='implement')
        await runner._tasks[j.job_id]
        assert j.status == 'needs_input'
        assert not any(e['event'] == 'writer_no_changes' for e in j.events)
    def forbidden(*a):
        pytest.fail('unchanged-writer check ran for question')
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.fail_unchanged_writer', forbidden)
    monkeypatch.setattr('shutil.which', lambda name, **kw: name)
    asyncio.run(run())
