"""Slow model turns and observable child CLIs are not three-minute stalls."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.spawner.run_watchdog import Watchdog, thresholds


@pytest.mark.parametrize('role', ['review', 'implement'])
def test_silent_model_turn_survives_three_minutes(role):
    limits = thresholds(role=role)
    assert limits['stuck_s'] > limits['stall_s']
    gate = Watchdog(limits, 0, None, writer=role == 'implement')
    assert gate.decide(0, None, None, 0, stdout_activity=0) is None
    assert gate.decide(300, None, None, 0, stdout_activity=0) is None


def test_card_controls_idle_limit_without_extending_hard_wall():
    limits = thresholds({'stuck_min': 8, 'max_min': 10}, role='review')
    assert limits['stuck_s'] == 480
    gate = Watchdog(limits, 0, None, writer=False)
    assert gate.decide(0, None, None, 0) is None
    assert gate.decide(479, None, None, 0) is None
    assert gate.decide(480, None, None, 0)['rule'] == 'stuck'
    assert gate.decide(600, None, None, 0)['rule'] == 'max'


@pytest.mark.parametrize('value', [0, -1, True, '2', float('inf'), float('nan')])
def test_invalid_card_idle_limit_is_refused(value):
    from plangraph.routing_errors import SpawnRefused
    with pytest.raises(SpawnRefused, match='stuck_min'):
        thresholds({'stuck_min': value})


def test_live_child_counts_as_activity_but_keeps_hard_deadline():
    gate = Watchdog(thresholds({'stuck_min': 1, 'stall_min': 1, 'max_min': 10}, role='implement'),
                    0, 'same', writer=True)
    assert gate.decide(0, 'same', 0, 0) is None
    assert gate.decide(300, 'same', 0, 0, waiting=True) is None
    assert gate.decide(600, 'same', 0, 0, waiting=True)['rule'] == 'max'


@pytest.mark.parametrize('state,expected', [('S', True), ('R', True), ('Z', False)])
def test_cpu_sample_records_visible_live_child(monkeypatch, state, expected):
    from plangraph.spawner import run_cpu
    rows = {100: (1, 100, 0, 'S'), 101: (100, 100, 0, state)}
    monkeypatch.setattr(run_cpu.sys, 'platform', 'linux')
    monkeypatch.setattr(run_cpu, 'proc_scan', lambda: rows)
    job = SimpleNamespace()
    runner = SimpleNamespace(jobs={'job': job})
    assert asyncio.run(run_cpu.cpu_seconds(runner, 'job', 100, 1)) == 0
    assert job._watchdog_child_active is expected
