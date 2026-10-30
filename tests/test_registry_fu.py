"""SPAWNER-REGISTRY-ROUTE-FU (D-3226): follow-ups after PR #253."""
from __future__ import annotations

import io
import json

import pytest

import plangraph.spawner.cli as cli
from plangraph.spawner.service import SpawnService


def _fake_supervisor(envelope, seen):
    class Supervisor:
        def __init__(self, *a, **kw):
            self.stdin = io.StringIO()
            self.stdin.close = lambda: None
            self.stdout = self
        def readline(self):
            seen.append(json.loads(self.stdin.getvalue()))
            return json.dumps(envelope) + '\n'
        def close(self):
            pass
    return Supervisor


@pytest.mark.parametrize('statuses, code', [(['running', 'refused'], 0), (['refused', 'refused'], 1),
                                            (['failed', 'refused'], 1), (['dry_run', 'dry_run'], 0)])
def test_cli_spawn_seats_prints_group(tmp_path, monkeypatch, capsys, statuses, code):
    # Live proof on main 7062cc54: `spawn --seats` exited 1 printing "'status'" (cli.py read
    # result['status'] on the seat-group envelope).
    envelope = {'seat_group': 'seats-abc', 'seat_coverage': [],
                'seats': [{'run_id': f'run-{i}', 'status': s} for i, s in enumerate(statuses)]}
    seen = []
    monkeypatch.setattr(cli, '_start_supervisor', _fake_supervisor(envelope, seen))
    monkeypatch.setattr(SpawnService, 'route', lambda self, *a, **kw: None)
    monkeypatch.setattr(SpawnService, 'result', lambda self, run_id: {'run_id': run_id, 'status': 'completed'})
    rc = cli.main(['--state', str(tmp_path), 'spawn', 'x', '--card', '{"estimate_tokens":1000}',
                   '--role', 'review', '--writer-family', 'claude', '--seats', '--wait', '0.05'])
    out = json.loads(capsys.readouterr().out)
    assert rc == code
    assert out['seat_group'] == 'seats-abc' and seen[0]['seats'] is True
    # --wait polls each running seat; refused seats keep their record.
    # r2 seat (qwen run-9ee7830a): a planned (dry_run) group exits 0 like a single dry run.
    assert [r['status'] for r in out['seats']] == ['completed' if s == 'running' else s for s in statuses]


def test_excluded_writer_family_override_leaves_no_audit_line(tmp_path):
    # Proof seats on 7062cc54 (qwen run-653409e3, mimo-pro run-31d4be8a): the excluded-family
    # refusals ran after hand_picked, so a refused override still wrote its audit line.
    import asyncio
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnRefused
    leg = DEFAULT_LEGS[0]
    service = SpawnService(state=tmp_path)
    with pytest.raises(SpawnRefused, match='excluded writer family'):
        asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x', role='review', model=leg.model,
                                  writer_families=[leg.family], override_reason='operator selection', dry_run=True))
    assert not (tmp_path / 'spawn-overrides.jsonl').exists()


def test_cli_route_error_saves_one_refused_record(tmp_path):
    # cli.py saved a second refusal after service.route() had already saved one.
    rc = cli.main(['--state', str(tmp_path), 'spawn', 'x', '--card', '{"estimate_tokens":1000}', '--role', 'review'])
    assert rc == 2
    assert len(list((tmp_path / 'runs').glob('run-*/record.json'))) == 1


def test_plan_spawn_review_without_writer_refuses():
    from plangraph.planner.routing import SpawnRefused, plan_spawn
    with pytest.raises(SpawnRefused, match='review needs a writer family'):
        plan_spawn({'estimate_tokens': 1000}, role='review', writer_families=())


def test_long_seat_verdict_kept_whole(tmp_path):
    # Live: qwen run-3654dec2 answered 6749 bytes; the record kept the last 4096, cutting the verdict.
    from plangraph.spawner.service import OUTPUT_KEEP
    service = SpawnService(state=tmp_path)
    verdict = 'VERDICT: CHANGES\n' + 'finding line\n' * 600
    service._save(dict(run_id='run-abc', status='completed', ended=1.0, stdout_tail=verdict,
                       attempts=[{'leg': 'x', 'status': 'completed', 'stdout_tail': verdict * 20}]))
    stored = service.result('run-abc')
    assert stored['stdout_tail'] == verdict
    attempt = stored['attempts'][0]['stdout_tail']
    assert attempt.startswith('VERDICT: CHANGES') and '[... output truncated ...]' in attempt
    assert len(attempt.encode()) <= OUTPUT_KEEP + 64


def test_card_refusal_names_missing_estimate_tokens():
    # D-3234 (d): the refusal names the missing field.
    from plangraph.planner.routing import SpawnRefused, plan_spawn
    with pytest.raises(SpawnRefused, match='card required: missing estimate_tokens'):
        plan_spawn({'risk': 'normal'}, role='implement')
    with pytest.raises(SpawnRefused, match='^card required$'):
        plan_spawn({}, role='implement')


def test_routing_skill_says_ask_route():
    # D-3209: AutoOS is read-only; the skill sends model picks to the spawner's route.
    from pathlib import Path
    text = Path(__file__).resolve().parents[1].joinpath('skills/routing-registry/SKILL.md').read_text()
    assert 'ask `route`' in text and 'plangraph.spawner.cli route' in text and 'D-3209' in text


def test_opencode_go_qwen_never_a_seat(tmp_path, monkeypatch):
    # D-3225: opencode-go/qwen3.8-flash via OmniRoute = empty content 6/6 (~289 s).
    # Not in the catalog today; the guard holds once a registry row admits it.
    import asyncio
    from plangraph.planner import routing
    from plangraph.planner.legs import Leg
    from plangraph.planner.routing import SpawnRefused, plan_spawn
    monkeypatch.setattr(routing, 'validate_model', lambda model: None)
    bad = Leg('og-qwen', 'subscription', 'opencode', 'opencode-go/qwen3.8-flash', 'qwen',
              ('128k',), 100000, False, roles=('review',))
    plan = plan_spawn({'estimate_tokens': 1000}, role='review', writer_families=('claude',), legs=(bad,),
                      leg_reader=lambda *a, **kw: {'state': 'available', 'evidence': None})
    assert not plan.legs and plan.skipped[0]['reason'] == 'no_verdict_seat'
    service = SpawnService(state=tmp_path)
    with pytest.raises(SpawnRefused, match='no-verdict seat'):
        asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x', role='review',
                                  model='opencode-go/qwen3.8-flash', writer_families=['claude'],
                                  override_reason='operator selection', dry_run=True))
    assert not (tmp_path / 'spawn-overrides.jsonl').exists()


def test_mcp_card_schema_names_estimate_tokens():
    # D-3244: live callers sent cards without estimate_tokens and got a bare 'card required'.
    from plangraph.spawner.mcp import TOOLS_METADATA
    from plangraph.planner.routing import SpawnRefused, plan_spawn
    schemas = {t['name']: t['inputSchema']['properties']['card'] for t in TOOLS_METADATA
               if 'card' in t['inputSchema']['properties']}
    assert {'plangraph_agent_spawn', 'plangraph_agent_route'} <= set(schemas)
    assert all(s['required'] == ['estimate_tokens'] for s in schemas.values())
    with pytest.raises(SpawnRefused, match='missing estimate_tokens'):
        plan_spawn({'risk': 'high'}, role='review', writer_families=['claude'])
