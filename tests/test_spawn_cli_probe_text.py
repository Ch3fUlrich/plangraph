"""CLI output and exit status for probe sweeps."""
import asyncio
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state
from plangraph.spawner.cli import main
from plangraph.spawner.leg_state import LegStateStore


@pytest.mark.parametrize('cooldown, until', [
    ('reset after 1h', '1970-01-01T01:16:40+00:00'),
    ('no reset supplied', 'unknown'),
])
def test_probe_text_skips_limited_leg(tmp_path, monkeypatch, capsys, cooldown, until):
    from plangraph.planner import legs
    from plangraph.spawner import cli, leg_probe
    store = LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)
    leg = legs.Leg('zz-limited', 'free', 'agy', 'zz-model', 'other',
                   ('128k',), 1000, False, roles=('implement',))
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (leg,))
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **kw: '/bin/agy')
    store.record_harness_limit('agy/zz-model', 'error: quota threshold ' + cooldown, 'test')
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(
        runner=SimpleNamespace(leg_reader=store)))
    assert main(['probe', '--leg', 'zz-limited']) == 0
    assert capsys.readouterr().out == f'zz-limited skipped=limited until {until}\n'


def test_probe_text_reset_overflow_prints_unknown(tmp_path, monkeypatch, capsys):
    """SPAWNER-RESET-AFTER r2: a legacy out-of-range reset_at never raises from
    the CLI -- the skip line prints 'unknown'."""
    from plangraph.spawner import cli, leg_probe

    async def fake(runner, legs, all_writers, timeout):
        return dict(ok=True, limited_legs=['zz-limited'],
                    probes=[dict(id='zz-limited', ok=False, reason='limited',
                                           seconds=0.0, infra=False, skipped='limited',
                                           reset_at=1.7e12)])

    monkeypatch.setattr(leg_probe, 'probe_many', fake)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(runner=SimpleNamespace()))
    assert leg_probe.reset_iso(1.7e12) == 'unknown'  # OverflowError/ValueError guarded
    assert main(['probe', '--leg', 'zz-limited']) == 0
    assert capsys.readouterr().out == 'zz-limited skipped=limited until unknown\n'


@pytest.mark.parametrize('probe_ok, exit_code', [(True, 0), (False, 1), (None, 0)],
                         ids=['healthy', 'failed', 'all-limited'])
def test_scheduled_probe_exit_status(tmp_path, monkeypatch, capsys, probe_ok, exit_code):
    from plangraph.planner import legs
    from plangraph.spawner import cli, leg_probe

    selected = tuple(legs.Leg(f'fixture-{n}', 'free', 'agy', f'fixture-model-{n}',
                              'other', ('128k',), 1000, False, roles=('review',))
                     for n in range(2))
    monkeypatch.setattr(legs, 'spawnable_legs', lambda: selected)
    monkeypatch.setattr(legs, 'routed_review_legs', lambda: selected)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **kw: '/bin/agy')
    monkeypatch.setattr(leg_probe, '_check_manage_key', lambda runner: None)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    limited = selected if probe_ok is None else selected[:1]
    for leg in limited:
        store.record_harness_limit(leg_probe.key(leg), 'error: quota threshold reset after 1h', 'test')
        store.record_probe_fail(leg_probe.key(leg), 'still limited', 'review-probe')
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(
        runner=SimpleNamespace(leg_reader=store)))
    probed = []

    async def probe(runner, leg, **kwargs):
        probed.append(leg.id)
        return dict(ok=probe_ok, reason='' if probe_ok else 'failed', seconds=0.0, infra=False)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    actual_exit = main(['--state', str(tmp_path), 'probe', '--all-reviewers', '--json'])
    reply = json.loads(capsys.readouterr().out)
    assert actual_exit == exit_code
    assert reply['ok'] is (exit_code == 0)
    assert reply['limited_legs'] == [leg.id for leg in limited]
    assert probed == [leg.id for leg in selected if leg not in limited]


def test_scheduled_probe_reports_one_outage_and_rearms_after_recovery(tmp_path, monkeypatch, capsys):
    from plangraph.planner import legs
    from plangraph.planner.routing import plan_spawn
    from plangraph.routing_errors import SpawnRefused
    from plangraph.routing_support.liveness import require_available
    from plangraph.spawner import cli, leg_probe
    from plangraph.spawner.runner import SpawnerRunner

    low, medium, limited = (next(leg for leg in legs.routed_review_legs() if leg.id == leg_id)
                            for leg_id in (legs.REVIEW_ARGON_LOW, legs.REVIEW_ARGON, legs.REVIEW_GPT))
    monkeypatch.setattr(legs, 'routed_review_legs', lambda: (low, medium, limited))
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda name, **kw: '/bin/' + name)
    monkeypatch.setattr(leg_probe, '_check_manage_key', lambda runner: None)
    monkeypatch.setattr("plangraph.planner.seats.head_writer_families", lambda card: [])
    clock = [10_000]
    monkeypatch.setattr(leg_state.time, 'time', lambda: clock[0])
    monkeypatch.setenv('PLANGRAPH_LEG_DOWN_S', '120')
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_harness_limit(leg_probe.key(limited), 'error: usage limit reached',
                               'test', down_s=7200, harness='codex')
    failing = {low.model}
    launched = []
    answer = 'OK'

    async def spawn(**kwargs):
        model = kwargs['model_or_combo']
        launched.append(model)
        if model in failing:
            return SimpleNamespace(job_id='j', status='failed', exit_code=1)
        return SimpleNamespace(job_id='j', status='completed', exit_code=0, stdout=answer)

    async def cancel(job_id):
        pass

    runner = SimpleNamespace(leg_reader=store, spawn=spawn, cancel=cancel)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(runner=runner))

    def sweep(expected_exit, *args):
        assert cli.main(['probe', '--json', *(args or ('--all-reviewers',))]) == expected_exit
        return json.loads(capsys.readouterr().out)

    def alerts():
        with sqlite3.connect(store.path) as db:
            return db.execute("SELECT leg_key FROM leg_events WHERE source='review-probe-outage' ORDER BY id").fetchall()

    def routed(reader=store, candidates=(low, medium)):
        plan = plan_spawn({'estimate_tokens': 1000, 'diff_ref': {'repo': str(tmp_path), 'base': 'a', 'head': 'b'}},
                          role='review', writer_families=('gpt',), legs=candidates,
                          leg_reader=reader, catalog_rows=())
        return {leg.id for leg in plan.legs}

    sweep(1)
    assert routed() == {low.id, medium.id}
    assert alerts() == []
    clock[0] += 1
    sweep(0)
    assert routed() == {medium.id}
    assert routed(candidates=(low, replace(medium, model=low.model))) == {medium.id}
    with pytest.raises(SpawnRefused, match=f'leg down: {low.id}'):
        require_available(low, store, {})
    require_available(medium, store, {})
    assert alerts() == [(low.id,)]
    deadline = store.review_probe_outage(low.id)['retry_at']
    launched.clear()
    clock[0] = deadline - 1
    skipped = sweep(0)
    assert [(r['id'], r.get('skipped')) for r in skipped['probes']] == [
        (low.id, 'down'), (medium.id, None), (limited.id, 'limited')]
    assert launched == [medium.model]
    with monkeypatch.context() as caller:
        heads = iter([{'cwd': 'a'}, {'cwd': 'b'}])
        caller.setattr(leg_probe, 'caller_heads', lambda: next(heads))
        rows = sweep(1)['probes']
        assert rows[0]['skipped'] == 'down' and rows[1]['state'] == 'caller-repo-leak'
    failing.add(medium.model)
    sweep(1)
    assert alerts() == [(low.id,)]
    failing.remove(medium.model)

    # A verified gateway response is quota evidence and cannot lift a review outage.
    gateway = SpawnerRunner()
    gateway.jobs['j'] = object()
    gateway._adapter_tokens['j'] = token = object()
    gateway.provider_response('j', leg_probe.key(low), 200, '', {}, source='gateway_adapter',
                              adapter_token=token, store=store)
    assert routed() == {medium.id}

    clock[0] = deadline
    assert routed() == {medium.id}
    assert not sweep(0)['probes'][0]['ok']
    retry = store.review_probe_outage(low.id)['retry_at']
    assert retry > deadline and routed() == {medium.id}
    assert alerts() == [(low.id,)]
    sweep(1, '--leg', low.id)
    failing.clear()
    diagnostic = asyncio.run(leg_probe.probe_many(runner, [low.id], role='review'))
    assert diagnostic['probes'][0]['ok'] and routed() == {medium.id}
    answer = 'unexpected answer'
    assert not sweep(1, '--leg', low.id)['probes'][0]['ok']
    assert routed() == {medium.id}
    answer = 'OK'
    clock[0] = retry
    assert sweep(0)['probes'][0]['ok']
    assert routed() == {low.id, medium.id}
    failing.add(low.model)
    clock[0] += 1
    sweep(1)
    clock[0] += 1
    sweep(0)
    assert alerts() == [(low.id,), (low.id,)]
    failing.clear()
    assert sweep(0, '--leg', low.id)['probes'][0]['ok']
    assert routed() == {low.id, medium.id}
