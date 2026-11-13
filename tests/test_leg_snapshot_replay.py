"""Replay the recorded roster through routing; external inputs are synthetic."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner import routing
from plangraph.routing_support import resolver
from plangraph.routing_support.leg_state import LegAdapter, LegStateStore
from plangraph.spawner import leg_probe
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.notify import FileInbox
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
        # The fixture has no exception text; this is an injected outage.
        raise RuntimeError('replay-injected snapshot failure; recorded exception unavailable')

    store = LegStateStore(tmp_path / 'leg-state.sqlite')
    roster = tuple(by_id[row['leg']] for row in rows)
    models = tuple({model for leg in roster for model in (leg.model, leg_probe.key(leg))})
    reader = LegAdapter(store, SimpleNamespace(snapshot=snapshot), models=models,
                        enabled=True)
    store.record_provider_response(roster[0].model, 429, 'synthetic replay limit',
                                    {'retry-after': '600'}, 'replay-input')
    # Synthetic recent provider evidence for the later fallback, not a status stub
    # or a claim that the recorded response had a served-model witness.
    store.record_probe_pass(leg_probe.key(opus), 'replay-input')
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(dict(off_families=['glm'])))
    monkeypatch.setenv('PLANGRAPH_ROUTE_POLICY', str(policy))
    host_root = tmp_path / 'host-config'
    host_root.mkdir()
    (host_root / 'opencode.jsonc').write_text('{"providers": {}}')
    monkeypatch.setenv('AUTOOS_ROOT', str(host_root))
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', roster)
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'catalog_rows', ())
    # Exercise real host admission with an injected standalone-model listing.
    monkeypatch.setattr(resolver, 'served_models', lambda *a, **k:
        frozenset(model for model in models if model != 'omniroute/deepseek-flash'))
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

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(runner, 'spawn', response)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    runner.default_cwd = str(tmp_path)
    svc = spawn_service(runner, state=tmp_path / 'state', leg_reader=reader,
                        keeper_sink=FileInbox(tmp_path / 'keeper-inbox'))
    card = {'estimate_tokens': 1000}
    plan = svc._route(card, 'review', ['gpt'], benchmark={})
    assert [leg.id for leg in plan.legs] == [row['leg'] for row in
        [rows[3], rows[2], *rows[4:6], *rows[15:]]]
    assert plan.review_seats == (errors[0], roster[2]) and plan.error is None
    run = asyncio.run(svc.spawn(card=card, prompt='Review the supplied document.', role='review',
        writer_families=['gpt'], benchmark={}, wait_seconds=None))
    attempts = [dict(leg=a['leg']['id'] if isinstance(a['leg'], dict) else a['leg'],
                     status=a['status'], skip_reason=a.get('skip_reason')) for a in run['attempts']]
    chosen = (run.get('chosen_leg') or {}).get('id')
    assert attempts != rows, (
        f'all 19 recorded rows reproduced; chosen={chosen}; probes={probes}')
    expected = errors if serving is None else errors[:serving_index + 1]
    # Fails on origin/main: the real route admits the candidates but reviews never probe.
    assert probes == [leg.id for leg in expected], (
        f'expected snapshot recovery probes; chosen={chosen}; probes={probes}; attempts={attempts}')
    assert run['status'] == 'completed', run.get('error')
    assert chosen == (serving or opus).id
    assert reads == [1, 1]
    dispatched = [leg.id for leg in expected] + ([] if serving else [opus.id])
    # Routing, not recorded statuses, generates every policy skip and all 19 attempts.
    # Host skips precede dispatch; planner exclusions are appended at final save.
    expected_order = ([rows[2]['leg']] + dispatched + [rows[0]['leg'], rows[1]['leg']]
                      + [row['leg'] for row in rows[6:15]]
                      + ([errors[1].id] if serving_index == 0 else [])
                      + ([opus.id] if serving else []) + [row['leg'] for row in rows[15:]])
    assert len(attempts) == len({row['leg'] for row in attempts}) == 19
    assert [row['leg'] for row in attempts] == expected_order
    actual = {row['leg']: row for row in attempts}
    for row in rows[:3] + rows[6:]:
        assert actual[row['leg']] == row
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
    assert saved['completion']['event'] == 'DONE'
    assert len(runner.calls) == 1 and runner.calls[0]['prompt'] == 'Review the supplied document.'
