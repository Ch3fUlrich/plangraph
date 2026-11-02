"""The spawner CLI text output for probe rows (SPAWNER-RESET-AFTER r1): a limited leg is skipped, not probed."""
import pytest

from plangraph.spawner.cli import main
from plangraph.spawner.leg_state import LegStateStore


@pytest.mark.parametrize('cooldown, until', [
    ('reset after 1h', '1970-01-01T01:16:40+00:00'),
    ('no reset supplied', 'unknown'),
])
def test_probe_text_skips_limited_leg(tmp_path, monkeypatch, capsys, cooldown, until):
    from types import SimpleNamespace
    from plangraph.planner import legs
    from plangraph.spawner import cli
    store = LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)
    leg = legs.Leg('zz-limited', 'free', 'agy', 'zz-model', 'other',
                   ('128k',), 1000, False, roles=('implement',))
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (leg,))
    store.record_harness_limit('agy/zz-model', 'error: quota threshold ' + cooldown, 'test')
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(
        runner=SimpleNamespace(leg_reader=store)))
    assert main(['probe', '--leg', 'zz-limited']) == 1
    assert capsys.readouterr().out == f'zz-limited skipped=limited until {until}\n'


def test_probe_text_reset_overflow_prints_unknown(tmp_path, monkeypatch, capsys):
    """SPAWNER-RESET-AFTER r2: a legacy out-of-range reset_at never raises from
    the CLI -- the skip line prints 'unknown'."""
    from types import SimpleNamespace
    from plangraph.spawner import cli, leg_probe

    async def fake(runner, legs, all_writers, timeout):
        return dict(ok=False, probes=[dict(id='zz-limited', ok=False, reason='limited',
                                           seconds=0.0, infra=False, skipped='limited',
                                           reset_at=1.7e12)])

    monkeypatch.setattr(leg_probe, 'probe_many', fake)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(runner=SimpleNamespace()))
    assert leg_probe.reset_iso(1.7e12) == 'unknown'  # OverflowError/ValueError guarded
    assert main(['probe', '--leg', 'zz-limited']) == 1
    assert capsys.readouterr().out == 'zz-limited skipped=limited until unknown\n'
