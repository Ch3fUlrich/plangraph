"""CLI output and exit status for probe sweeps."""
import json
from types import SimpleNamespace

import pytest

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
