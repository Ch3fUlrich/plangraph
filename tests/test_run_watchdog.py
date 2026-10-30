"""Run watchdog decisions and platform sampling contracts."""
import math
import pytest

from plangraph.spawner.run_watchdog import Watchdog, thresholds, is_activity, cpu_seconds
from plangraph.planner.routing import SpawnRefused


@pytest.mark.parametrize('value', [0, -1, True, '2', math.inf, math.nan])
def test_bad_card(value):
    with pytest.raises(SpawnRefused):
        thresholds({'stall_min': value})


def test_activity():
    assert not is_activity('\x1b[0m> build · cx-gpt-6.1-sol')
    assert not is_activity('  ')
    assert is_activity('\x1b[0m$ \x1b[0mgit status')


def test_rules():
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 3})
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(60, ('files', 'head'), None, 100)['rule'] == 'no-activity'
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(120, ('files', 'head'), 119, 100)['rule'] == 'no-first-edit'
    w = Watchdog(limits, 0, ('files', 'head'))
    assert w.decide(1, ('changed', 'head'), None, 1) is None
    assert w.decide(62, ('changed', 'head'), None, 1)['rule'] == 'no-progress'
    assert w.decide(180, ('changed', 'head'), 180, 200)['rule'] == 'max'


@pytest.mark.asyncio
async def test_windows_cpu_fake():
    class Api:
        def cpu_seconds(self, handle):
            assert handle == 42
            return 3.5
    class Runner:
        _win_jobs = {'j': 42}
        win_job_api = Api()
    assert await cpu_seconds(Runner(), 'j', 1, 0) == 3.5
