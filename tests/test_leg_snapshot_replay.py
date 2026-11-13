"""Replay the recorded decisions; injected reads/probe/witnesses are not telemetry."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.routing_support import resolver
from plangraph.routing_support.leg_state import LegAdapter, LegStateStore
from plangraph.spawner import leg_probe
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.service import SpawnService
from registry_route_util import _Runner


@pytest.mark.parametrize('serving_index', [0, 1, None], ids=['gemini', 'sonnet', 'failed'])
def test_recorded_snapshot_attempt_order_retries_and_probes(tmp_path, monkeypatch, serving_index):
    rows = json.loads((Path(__file__).parent / 'fixtures' /
        'leg_snapshot_attempts.json').read_text())
    assert len(rows) == 19 and all(set(row) == {'leg', 'status', 'skip_reason'} for row in rows)
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    errors = [by_id[row['leg']] for row in rows if row['skip_reason'] == 'leg_state_snapshot_error']
    opus = by_id[next(row['leg'] for row in rows if row['status'] == 'completed')]
    serving = errors[serving_index] if serving_index is not None else None
    reads, probes = [], []

    def snapshot():
        reads.append(1)
        # The record has NO exception text. This error is a replay input only.
        raise RuntimeError('replay-injected snapshot failure; recorded exception unavailable')

    store = LegStateStore(tmp_path / 'leg-state.sqlite')
    reader = LegAdapter(store, SimpleNamespace(snapshot=snapshot), models=(), enabled=True)
    read = reader.leg_status
    recorded = {leg_probe.key(by_id[row['leg']]): row for row in rows}

    def admission(model, **scope):
        if model in {leg_probe.key(leg) for leg in errors}:
            return read(model, **scope)
        # Replay only the other recorded admission decisions, not invented state.
        row = recorded[model]
        return (dict(state='ok') if row['skip_reason'] in (None, 'earlier_leg_succeeded')
                else dict(state='limited', reason=row['skip_reason']))

    reader.leg_status = admission
    runner = _Runner()
    spawn = runner.spawn

    async def response(**kwargs):
        job = await spawn(**kwargs)
        job.exit_code = 0
        job.stdout, job.response_finish_reason = 'VERDICT: PASS\nTEST_GAMING: no\n', 'stop'
        if serving and kwargs['model_or_combo'] == serving.model:
            job.served_model, job.served_family = serving.model, serving.family
            job.served_witness_reason = 'injected response-side replay evidence'
        return job

    async def probe(_runner, leg, **kwargs):
        assert kwargs['store'] is store
        probes.append(leg.id)
        return dict(ok=leg == serving, reason='replay-injected probe timeout')

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '0')
    monkeypatch.setattr(runner, 'spawn', response)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **k: None)
    monkeypatch.setattr(SpawnService, '_leg_prompt', lambda *a, **k: 'review')
    monkeypatch.setattr('plangraph.spawner.notify.completion_event', lambda *a: {})
    svc = spawn_service(runner, state=tmp_path / 'state', leg_reader=reader)
    # Preserve the recorded order and policy skips; replay the real dispatch loop.
    legs = tuple(by_id[row['leg']] for row in rows[:6] + rows[15:])
    plan = SpawnPlan(legs, tuple(dict(leg=row['leg'], reason=row['skip_reason'])
        for row in rows[6:15]), 'recorded-attempt replay', (errors[0],), ())
    run = asyncio.run(svc.spawn(card={'estimate_tokens': 1000}, role='review',
        writer_families=['gpt'], _plan=plan, wait_seconds=None))
    attempts = [dict(leg=a['leg']['id'] if isinstance(a['leg'], dict) else a['leg'],
                     status=a['status'], skip_reason=a.get('skip_reason')) for a in run['attempts']]
    # On origin/main this fails with EXACTLY all 19 rows and Opus chosen, no probes.
    assert attempts != rows, (
        f"all 19 recorded rows reproduced; chosen={run['chosen_leg']['id']}; probes={probes}")
    assert run['status'] == 'completed', run.get('error')
    assert run['chosen_leg']['id'] == (serving or opus).id
    assert attempts[:3] == rows[:3] and attempts[-4:] == rows[15:]
    assert [row for row in attempts if row['leg'] in {r['leg'] for r in rows[6:15]}] == rows[6:15]
    expected = errors if serving is None else errors[:serving_index + 1]
    assert probes == [leg.id for leg in expected] and reads == [1, 1]
    assert [row['leg'] for row in attempts[:3 + len(expected)]] == [
        row['leg'] for row in rows[:3]] + [leg.id for leg in expected]
    for leg in expected:
        attempt = next(row for row in attempts if row['leg'] == leg.id)
        if leg == serving:
            assert attempt['status'] == 'completed' and attempt['skip_reason'] is None
        else:
            assert attempt['status'] == 'skipped'
            assert attempt['skip_reason'] == (
                'leg_state_snapshot_error: RuntimeError: replay-injected snapshot failure; '
                'recorded exception unavailable; probe: replay-injected probe timeout')
    if serving:
        assert run['served_model'] == serving.model and run['served_family'] != 'gpt'
        assert all(call['model_or_combo'] != opus.model for call in runner.calls)
        assert next(row for row in attempts if row['leg'] == opus.id)['skip_reason'] == 'earlier_leg_succeeded'
    else:
        assert run['served_model'] == 'unwitnessed'
    saved = svc.result(run['run_id'])
    assert saved['attempts'] == run['attempts'] and saved['chosen_leg'] == run['chosen_leg']
