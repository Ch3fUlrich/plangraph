"""Bounded record collisions and reviewed-writer identity, without a harness."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.routing_support import private_io
from plangraph.spawner import cli
from plangraph.spawner.runner import spawn_service as DispatchService
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('verb', ['wait', 'result', 'status'])
@pytest.mark.parametrize('failures', [2, 100])
@pytest.mark.parametrize('winerror', [None, 5, 32, 33])
def test_record_collision(tmp_path, monkeypatch, capsys, verb, failures, winerror):
    service = DispatchService(state=tmp_path)
    record = dict(run_id='run-collision', status='completed', ended=1, exit_code=0)
    calls, sleeps = [], []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == service._path(record['run_id']):
            calls.append(1)
            if len(calls) <= failures:
                error = PermissionError(13, 'private detail', '/private/record.json')
                if winerror is not None:
                    error.winerror = winerror
                raise error
            return json.dumps(record)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(private_io, 'time', SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    code = cli.main(['--state', str(tmp_path), verb, record['run_id']])
    out, err = capsys.readouterr()
    if winerror is None:
        assert code != 0 and not out
        assert err == 'run-collision: PermissionError\n'
        assert len(calls) == 1
    elif failures == 2:
        assert code == 0 and json.loads(out) == record and not err
        assert len(calls) == 3
    else:
        assert code != 0 and not out
        assert err == 'run-collision: PermissionError\n'
        assert len(calls) == 20
    assert sleeps == [0.05] * (len(calls) - 1)


@pytest.mark.parametrize('dispatch', [False, True])
def test_given_family_and_review_of_review(tmp_path, monkeypatch, dispatch):
    from registry_route_util import _Runner, CARD
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan, SpawnRefused
    from plangraph.planner import routing

    service = (DispatchService if dispatch else SpawnService)(_Runner(), state=tmp_path)
    chosen = next(l for l in DEFAULT_LEGS if l.family == 'qwen')
    plan = SpawnPlan((chosen,), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'review')
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', _identity)
    record = asyncio.run(service.spawn(card=CARD, role='review', writer_families=['gpt'], dry_run=True))
    assert record['given_writer_families'] == ['gpt']
    assert record['writer_family'] == 'qwen'
    seen = []
    monkeypatch.setattr(routing, 'plan_spawn', lambda *a, **kw: seen.extend(kw['writer_families']) or plan)
    SpawnService._route(service, CARD, role='review', review_of=record['run_id'])
    assert seen == ['gpt']
    gpt = next(l for l in DEFAULT_LEGS if l.family == 'gpt' and l.tool_mode == 'tools')
    with pytest.raises(SpawnRefused, match='excluded writer family'):
        asyncio.run(service._spawn(card=CARD, role='review', review_of=record['run_id'], leg=gpt.id, dry_run=True))


async def _identity(plan, **kwargs):
    return plan


def test_record_collision_winerror33(tmp_path, monkeypatch, capsys):
    # The winerror=33 coverage is now in test_record_collision; also cover a
    # command without run_id, which must not disclose PermissionError paths.
    service = DispatchService(state=tmp_path)

    def denied():
        raise PermissionError(13, 'private detail', '/private/record.json')

    monkeypatch.setattr(service, 'ps', denied)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    assert cli.main(['--state', str(tmp_path), 'ps']) == 1
    out, err = capsys.readouterr()
    assert not out
    assert err == 'PermissionError\n'


@pytest.mark.parametrize('dispatch', [False, True])
@pytest.mark.parametrize('families', [['gpt', 'claude'], []])
def test_review_family_overlap_and_empty_fallback(tmp_path, monkeypatch, dispatch, families):
    from registry_route_util import _Runner, CARD
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan, SpawnRefused

    service = (DispatchService if dispatch else SpawnService)(_Runner(), state=tmp_path)
    chosen = next(l for l in DEFAULT_LEGS if l.family == 'qwen')
    plan = SpawnPlan((chosen,), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'review')
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', _identity)
    source = asyncio.run(service.spawn(card=CARD, writer_families=families, dry_run=True))
    source.update(given_writer_families=families, writer_family='gpt')
    service._save(source)
    record = asyncio.run(service.spawn(card=CARD, role='review', review_of=source['run_id'], dry_run=True))
    assert record['given_writer_families'] == (families or ['gpt'])
    from plangraph.planner import routing
    seen = []
    monkeypatch.setattr(routing, 'plan_spawn', lambda *a, **kw: seen.append(kw['writer_families']) or plan)
    SpawnService._route(service, CARD, role='review', review_of=record['run_id'])
    assert seen.pop() == (families or ['gpt'])
    gpt = next(l for l in DEFAULT_LEGS if l.family == 'gpt' and l.tool_mode == 'tools')
    if families:
        allowed = asyncio.run(service._spawn(card=CARD, role='review', review_of=source['run_id'], leg=gpt.id, dry_run=True, override_reason='verify subset family exclusion'))
        assert allowed['given_writer_families'] == families
    else:
        with pytest.raises(SpawnRefused, match='excluded writer family'):
            asyncio.run(service._spawn(card=CARD, role='review', review_of=source['run_id'], leg=gpt.id, dry_run=True))
    assert seen.pop() == (families or ['gpt'])


@pytest.mark.parametrize('dispatch', [False, True])
def test_missing_writer_family(tmp_path, monkeypatch, dispatch):
    from registry_route_util import _Runner, CARD
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan, SpawnRefused
    from plangraph.planner import routing

    service = (DispatchService if dispatch else SpawnService)(_Runner(), state=tmp_path)
    chosen = next(l for l in DEFAULT_LEGS if l.family == 'qwen')
    plan = SpawnPlan((chosen,), (), 'test', (), ())
    monkeypatch.setattr(service, 'result', lambda *a, **kw: dict(writer_family=None, given_writer_families=[]))
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'implement')
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', _identity)
    monkeypatch.setattr('plangraph.planner.legs.leg_active', lambda leg: True)
    seen = []
    monkeypatch.setattr(routing, 'plan_spawn', lambda *a, **kw: seen.append(kw['writer_families']) or plan)
    record = asyncio.run(service._spawn(card=CARD, review_of='run-missing', leg=chosen.id,
                                       dry_run=True, override_reason='test missing family'))
    assert seen == [[]]
    assert record['given_writer_families'] == []
    record = asyncio.run(service._spawn(card=CARD, review_of='run-missing', dry_run=True))
    assert record['given_writer_families'] == []
    with pytest.raises(SpawnRefused, match='writer family unavailable'):
        SpawnService._route(service, CARD, review_of='run-missing')
