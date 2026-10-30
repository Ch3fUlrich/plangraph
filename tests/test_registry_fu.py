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


@pytest.mark.parametrize('statuses, code', [(['running', 'refused'], 0), (['refused', 'refused'], 1)])
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
