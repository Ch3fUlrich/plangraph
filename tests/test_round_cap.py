import asyncio
import json

from types import SimpleNamespace

import pytest

from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.round_cap import RoundCapRefused, check_round_cap
from plangraph.spawner.service import SpawnService

ITEM = 'plangraph:ROUND-CAP-X'


@pytest.fixture(autouse=True)
def offline_keeper(monkeypatch):
    monkeypatch.setattr('plangraph.keeper.cli_verbs.Session.call', lambda *a, **kw: None)


def _seed(state, n, item=ITEM, pr='7', **extra):
    for i in range(n):
        d = state / 'runs' / f'run-{item.split(":")[1].lower().replace("-", "")}{pr}{i}'
        d.mkdir(parents=True)
        (d / 'record.json').write_text(json.dumps(dict(
            dict(run_id=d.name, role='implement', item_key=item, round_ref=pr, status='completed', writer_launched=True, started=i),
            **extra)))


def _svc(tmp_path):
    return SpawnService(object(), state=tmp_path)


def _card(item=ITEM, pr='7'):
    return dict(item_key=item, pr=pr, findings=['f'])


@pytest.mark.parametrize('prior', [0, 1, 2])
def test_rounds_one_to_three_allowed(tmp_path, prior):
    _seed(tmp_path, prior)
    check_round_cap(_svc(tmp_path), _card(), 'implement')


def test_fourth_round_refused_with_replan_payload(tmp_path):
    _seed(tmp_path, 3)
    with pytest.raises(RoundCapRefused) as err:
        check_round_cap(_svc(tmp_path), _card(), 'implement')
    body = json.loads(str(err.value))
    plan = body['replan_required']
    assert body['reason'] == 'round_cap'
    assert (plan['item'], plan['pr'], plan['rounds']) == (ITEM, '7', 3)
    assert plan['findings_source'] == 'card.findings'
    assert plan['findings'] == ['f']  # LOW: the open findings travel in the payload
    assert plan['replan_card']['role'] == 'research'


def test_spawn_refuses_before_any_run(tmp_path):
    _seed(tmp_path, 3)
    with pytest.raises(RoundCapRefused):
        asyncio.run(_svc(tmp_path)._spawn(card=_card(), prompt='x'))
    assert len(list((tmp_path / 'runs').glob('run-*'))) == 3


def test_other_item_pr_review_refused_and_dry_runs_unaffected(tmp_path):
    _seed(tmp_path, 3)
    _seed(tmp_path, 2, item='plangraph:OTHER', pr='9')
    svc = _svc(tmp_path)
    check_round_cap(svc, _card('plangraph:OTHER', '9'), 'implement')
    check_round_cap(svc, _card(pr='8'), 'implement')      # other PR of the same item
    check_round_cap(svc, _card(), 'review')                # only writers are capped
    _seed(tmp_path, 1, item='plangraph:DRY', pr='1', status='dry_run')
    _seed(tmp_path, 3, item='plangraph:REF', pr='1', status='refused')
    check_round_cap(svc, _card('plangraph:REF', '1'), 'implement')


def _stub_route(svc, plan=None, error=None):
    def route(card, role, *args, **kwargs):
        assert role == 'research'
        if error is not None:
            raise error
        return plan
    svc._route = route


def test_route_table_leg_is_reported(tmp_path):
    """MED-6: the leg and order come from the stubbed route plan, not a fallback."""
    _seed(tmp_path, 3)
    svc = _svc(tmp_path)
    legs = [SimpleNamespace(id='stub-leg-a'), SimpleNamespace(id='stub-leg-b')]
    _stub_route(svc, SimpleNamespace(first_leg=legs[0], legs=legs, error=None))
    with pytest.raises(RoundCapRefused) as err:
        check_round_cap(svc, _card(), 'implement')
    plan = err.value.payload
    assert (plan['replan_leg'], plan['replan_leg_order']) == ('stub-leg-a', ['stub-leg-a', 'stub-leg-b'])
    assert plan['replan_route_error'] is None


def test_route_refusal_is_named_not_substituted(tmp_path):
    _seed(tmp_path, 3)
    svc = _svc(tmp_path)
    _stub_route(svc, error=SpawnRefused('no planning leg up'))
    with pytest.raises(RoundCapRefused) as err:
        check_round_cap(svc, _card(), 'implement')
    plan = err.value.payload
    assert (plan['replan_leg'], plan['replan_leg_order']) == (None, [])
    assert plan['replan_route_error'] == 'no planning leg up'


def test_unexpected_route_error_surfaces(tmp_path):
    """MED-5: a wrong signature or missing method is not an expected routing failure."""
    _seed(tmp_path, 3)
    svc = _svc(tmp_path)
    _stub_route(svc, error=TypeError('bad signature'))
    with pytest.raises(TypeError):
        check_round_cap(svc, _card(), 'implement')


def test_runs_without_recorded_ref_do_not_count_toward_a_pr(tmp_path):
    """MED-4: three legacy runs with no round_ref must not cap a PR."""
    _seed(tmp_path, 3, pr='legacy', round_ref=None)
    check_round_cap(_svc(tmp_path), _card(pr='9'), 'implement')
    _seed(tmp_path, 3, pr='7')
    check_round_cap(_svc(tmp_path), _card(pr='9'), 'implement')


def test_card_without_ref_counts_only_ref_less_runs(tmp_path):
    _seed(tmp_path, 3, pr='7')
    check_round_cap(_svc(tmp_path), dict(item_key=ITEM), 'implement')
    _seed(tmp_path, 3, pr='x', round_ref=None)
    with pytest.raises(RoundCapRefused):
        check_round_cap(_svc(tmp_path), dict(item_key=ITEM), 'implement')


def test_public_spawn_refuses_before_remote_admission(tmp_path, monkeypatch):
    """LOW-7: the public spawn entry enforces the cap before overflow/remote admission."""
    _seed(tmp_path, 3)
    def admit(*args, **kwargs):
        raise AssertionError('admission ran before the round cap')
    monkeypatch.setattr('plangraph.spawner.seat_spawn.admit', admit)
    with pytest.raises(RoundCapRefused):
        asyncio.run(_svc(tmp_path).spawn(card=_card(), prompt='x'))


def test_environment_item_identity_cannot_bypass_round_cap(tmp_path, monkeypatch):
    _seed(tmp_path, 3)
    monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', ITEM)
    with pytest.raises(RoundCapRefused):
        check_round_cap(_svc(tmp_path), {'pr': '7'}, 'implement')


def test_prelaunch_provider_exhaustion_does_not_consume_rounds(tmp_path):
    _seed(tmp_path, 3, status='limited', writer_launched=False, attempts=[])
    check_round_cap(_svc(tmp_path), _card(), 'implement')


def test_concurrent_launches_cannot_take_the_same_final_round(tmp_path):
    from plangraph.planner.legs import DEFAULT_LEGS
    import os
    _seed(tmp_path, 2)
    chosen = next(l for l in DEFAULT_LEGS if 'implement' in l.roles)
    calls = []
    async def go():
        async def one(index):
            ident = f'job-{index}'
            runner = SimpleNamespace(default_cwd=str(tmp_path), _processes={}, _tasks={}, jobs={})
            async def spawn(**kwargs):
                calls.append(ident)
                await asyncio.sleep(.03)
                runner._processes[ident] = SimpleNamespace(pid=os.getpid())
                return SimpleNamespace(job_id=ident, status='completed', exit_code=0, events=[], survivors=[])
            runner.spawn = spawn
            service = SpawnService(runner, state=tmp_path)
            record = dict(run_id='run-' + f'{index:032x}', role='implement', item_key=ITEM,
                          round_ref='7', started=0, status='running', attempts=[])
            return await service._launch_leg(record, chosen, role='implement', prompt_record=None)
        return await asyncio.gather(one(0), one(1), return_exceptions=True)
    result = asyncio.run(go())
    assert len(calls) == 1 and sum(isinstance(r, RoundCapRefused) for r in result) == 1
    assert sum(not isinstance(r, BaseException) for r in result) == 1, result


def _reserve_worker(state, start, finish, results):
    import os
    from pathlib import Path
    from plangraph.spawner.round_cap import reserve_round, finish_round
    start.wait()
    record = dict(run_id=f'run-child-{os.getpid()}', item_key=ITEM, round_ref='7')
    token = reserve_round(SimpleNamespace(state=Path(state)), record)
    results.put(token is not False)
    finish.wait(15)
    if token is not False:
        finish_round(token, False)


def test_round_capacity_is_atomic_between_processes(tmp_path):
    import multiprocessing
    _seed(tmp_path, 2)
    ctx = multiprocessing.get_context('spawn')
    start, finish, results = ctx.Barrier(2), ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=_reserve_worker, args=(str(tmp_path), start, finish, results)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        assert sorted(results.get(timeout=20) for _ in children) == [False, True]
    finally:
        finish.set()
        for child in children:
            child.join(20)
            if child.is_alive():
                child.terminate()
                child.join()
        results.close()
    assert all(child.exitcode == 0 for child in children)


def test_failed_spawn_releases_unlaunched_capacity(tmp_path):
    from plangraph.spawner.round_cap import spawn_round
    _seed(tmp_path, 2)
    calls = []
    async def fail(**kwargs):
        calls.append(1)
        raise ValueError('failed before process creation')
    service = SimpleNamespace(state=tmp_path, runner=SimpleNamespace(spawn=fail))
    async def go():
        for index in range(2):
            record = dict(run_id=f'run-fail-{index}', role='implement', item_key=ITEM, round_ref='7')
            with pytest.raises(ValueError, match='before process'):
                await spawn_round(service, record, _card(), prompt_record=None)
    asyncio.run(go())
    assert len(calls) == 2
