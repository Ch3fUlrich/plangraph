"""Round-cap requests use spawner authority and leave L1 work to the L1."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.keeper.cli_verbs import Session, Unreachable
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.round_cap import RoundCapRefused, marker_path
from plangraph.spawner.service import SpawnService

ITEM = 'plangraph:ROUND-CAP-X'
FINDINGS = ['HIGH a.py:1 - first finding, verbatim', dict(text='MED b.py:2 - second finding')]


def _seed(state):
    for i in range(3):
        d = state / 'runs' / f'run-seed{i}'
        d.mkdir(parents=True)
        (d / 'record.json').write_text(json.dumps(dict(
            run_id=d.name, role='implement', item_key=ITEM, round_ref='7', status='completed', writer_launched=True, started=i)))


@pytest.fixture
def calls(monkeypatch):
    seen = []
    def call(self, method, verb, params=None, body=None):
        seen.append((method, verb, body))
        assert (method, verb) == ('POST', 'alarms'), 'spawner may only request a replan'
    monkeypatch.setattr(Session, 'call', call)
    return seen


def _service(tmp_path, error=None):
    _seed(tmp_path)
    service = SpawnService(object(), state=tmp_path)
    leg = SimpleNamespace(id='planning-leg')
    def route(*args, **kwargs):
        if error:
            raise error
        return SimpleNamespace(first_leg=leg, legs=[leg], error=None)
    service._route = route
    return service


def _cap(service, **extra):
    async def go():
        with pytest.raises(RoundCapRefused) as err:
            await service._spawn(card=dict(item_key=ITEM, pr='7', findings=FINDINGS, **extra), prompt='x')
        return err.value.payload
    return asyncio.run(go())


def test_cap_requests_l1_replan_without_spawning_or_creating_items(tmp_path, calls):
    service = _service(tmp_path)
    payload = _cap(service)
    assert payload['replan']['status'] == 'requested' and not payload['replan']['started']
    assert payload['replan_leg'] == 'planning-leg'
    assert calls[0][2]['card'] == ITEM and 'round_cap' in calls[0][2]['reason']
    assert service.tasks == {} and len(list((tmp_path / 'runs').glob('*'))) == 3
    saved = json.loads(marker_path(service, ITEM, '7').read_text())
    assert saved['status'] == 'requested'
    assert saved['request']['findings'] == [FINDINGS[0], FINDINGS[1]['text']]
    events = list((tmp_path / 'watchdog/outbox').glob('*.json'))
    assert len(events) == 1
    event = json.loads(events[0].read_text())
    assert event['recipient'] == 'routing' and event['replan_required']['findings'] == saved['request']['findings']
    assert 'L1:' in event['text'] and '<=300 lines' in event['text'] and '\n' not in event['text']


def test_second_refusal_requests_no_second_replan(tmp_path, calls):
    service = _service(tmp_path)
    _cap(service)
    assert _cap(service)['replan']['status'] == 'requested'
    assert len(calls) == 1


def test_outbox_failure_retries_delivery_without_reposting_keeper_alarm(tmp_path, calls, monkeypatch):
    from plangraph.spawner import stall_watchdog
    service = _service(tmp_path)
    deliver = stall_watchdog.deliver_event
    monkeypatch.setattr(stall_watchdog, 'deliver_event', lambda *a, **kw: (_ for _ in ()).throw(OSError('outbox')))
    assert _cap(service)['replan']['status'] == 'failed'
    assert json.loads(marker_path(service, ITEM, '7').read_text())['keeper_sent'] is True
    monkeypatch.setattr(stall_watchdog, 'deliver_event', deliver)
    assert _cap(service)['replan']['status'] == 'requested'
    assert len(calls) == 1
    assert len(list((tmp_path / 'watchdog/outbox').glob('*.json'))) == 1


def test_no_planning_leg_still_requests_l1_help(tmp_path, calls):
    payload = _cap(_service(tmp_path, SpawnRefused('no planning leg up')))
    assert payload['replan']['status'] == 'requested'
    assert payload['replan_route_error'] == 'no planning leg up' and len(calls) == 1


def test_keeper_failure_preserves_request_and_retries(tmp_path, calls, monkeypatch):
    service = _service(tmp_path)
    normal_call = Session.call
    def down(*args, **kwargs):
        raise Unreachable('offline')
    monkeypatch.setattr(Session, 'call', down)
    assert _cap(service)['replan']['status'] == 'failed'
    saved = json.loads(marker_path(service, ITEM, '7').read_text())
    assert saved['request']['findings'][0] == FINDINGS[0] and saved['reason'] == 'Unreachable'
    assert len(list((tmp_path / 'watchdog/outbox').glob('*.json'))) == 1
    monkeypatch.setattr(Session, 'call', normal_call)
    assert _cap(service)['replan']['status'] == 'requested' and len(calls) == 1


def test_request_preserves_execution_fence(tmp_path, calls):
    _cap(_service(tmp_path), workflow_id='wf', run_id='run', attempt=2)
    assert {key: calls[0][2][key] for key in ('workflow_id', 'run_id', 'attempt')} == dict(
        workflow_id='wf', run_id='run', attempt=2)


def test_spawner_can_alarm_but_cannot_define_slices(tmp_path, monkeypatch):
    from keeper_util import World, seed, defn
    from plangraph.keeper import auth
    from plangraph.keeper.errors import Refused
    with_world = tmp_path / 'keeper'
    world = World(with_world)
    try:
        seed(world, [('ROUND-CAP-X', [])], project='plangraph')
        token = auth.add_principal(world.conn, 'spawn', 'spawner', ['plangraph'])
        principal = auth.authenticate(world.conn, token)
        with pytest.raises(Refused):
            world.store.create_items(principal, [defn('SLICE', project='plangraph', under=ITEM)])
        def call(self, method, verb, params=None, body=None):
            assert (method, verb) == ('POST', 'alarms')
            world.store.worker_alarm(principal, body)
        monkeypatch.setattr(Session, 'call', call)
        payload = _cap(_service(tmp_path / 'spawner'))
        assert payload['replan']['status'] == 'requested'
        alarms = world.conn.execute('SELECT key,detail FROM projector_alarms').fetchall()
        assert len(alarms) == 1 and alarms[0]['key'] == ITEM and 'round_cap' in alarms[0]['detail']
    finally:
        world.close()


def test_request_explicitly_reports_automatic_launch_deferred(tmp_path, calls):
    payload = _cap(_service(tmp_path))
    assert payload['automatic_replan']['status'] == 'deferred'
    assert 'stronger model' in payload['automatic_replan']['reason']
    assert 'durable' in payload['automatic_replan']['reason']
    saved = json.loads(marker_path(SimpleNamespace(state=tmp_path), ITEM, '7').read_text())
    assert saved['request']['automatic_replan'] == payload['automatic_replan']
