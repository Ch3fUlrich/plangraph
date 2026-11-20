"""The affected-tests bound scales with the selection (LE-PREPUSH-AFFECTED-BOUND-SPAWNER-CORE)."""
import json
import subprocess
import sys

import pytest

from test_preflight import preflight, repo  # noqa: F401


def plan_script(tmp_path, count, body=''):
    script = tmp_path / 'affected_tests.py'
    script.write_text('import json, sys, time\n'
                      f'print(json.dumps({{"tests": ["t%d" % i for i in range({count})]}}), flush=True)\n'
                      'if "--run" in sys.argv:\n'
                      f'    {body or "pass"}\n')
    return script


@pytest.mark.parametrize('count,expected', [
    (0, 240), (100, 240), (120, 240), (223, 446), (570, 1140), (5000, preflight.MAX_TEST_TIMEOUT)])
def test_bound_scales_with_selected_paths_and_is_capped(tmp_path, count, expected):
    script = plan_script(tmp_path, count)
    command = [sys.executable, str(script), '--run']
    assert preflight.check_timeout(command, tmp_path, {}) == expected


def test_a_command_without_run_or_with_an_unreadable_plan_keeps_the_base_bound(tmp_path):
    assert preflight.check_timeout([sys.executable, str(plan_script(tmp_path, 900))], tmp_path, {}) == preflight.TEST_TIMEOUT
    broken = tmp_path / 'affected_tests.py'
    broken.write_text('print("not json")\n')
    assert preflight.check_timeout([sys.executable, str(broken), '--run'], tmp_path, {}) == preflight.TEST_TIMEOUT


def test_a_slow_large_selection_passes_where_the_flat_bound_failed(tmp_path, monkeypatch):
    """A selection whose run outlasts the old flat bound still passes inside the scaled one."""
    monkeypatch.setattr(preflight, 'TEST_TIMEOUT', 0.5)
    monkeypatch.setattr(preflight, 'TEST_PER_PATH_TIMEOUT', 0.05)
    monkeypatch.setattr(preflight, 'MAX_TEST_TIMEOUT', 20)
    script = plan_script(tmp_path, 40, 'time.sleep(1.5)')
    code, _ = preflight.run([sys.executable, str(script), '--run'], tmp_path, dict(__import__('os').environ))
    assert code == 0   # bound = max(0.5, 40 * 0.05) = 2 s > 1.5 s run


def test_the_scaled_bound_is_named_when_it_is_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, 'TEST_TIMEOUT', 0.5)
    monkeypatch.setattr(preflight, 'TEST_PER_PATH_TIMEOUT', 0.05)
    monkeypatch.setattr(preflight, 'MAX_TEST_TIMEOUT', 20)
    script = plan_script(tmp_path, 40, 'time.sleep(10)')
    code, text = preflight.run([sys.executable, str(script), '--run'], tmp_path, dict(__import__('os').environ))
    assert code == 1
    assert text == 'tests: affected-tests step exceeded its 2.0 second bound (40 selected test paths)\n'


@pytest.mark.parametrize('cores,mem_gib,expected', [
    (32, 110, 16), (32, 20, 10), (12, 3.8, 2), (2, 64, 2), (8, None, 4)])
def test_affected_workers_follow_cores_and_memory_never_fixed_at_two(cores, mem_gib, expected):
    """LE-PREPUSH-TESTS-ON-BUSIEST-HOST (3): the worker count comes from the host, between 2 and 16."""
    assert preflight.affected_workers({}, cores=cores, mem_gib=mem_gib) == expected


def test_affected_workers_override_and_meminfo(tmp_path):
    assert preflight.affected_workers({'PREFLIGHT_TEST_WORKERS': '6'}, cores=32, mem_gib=110) == 6
    assert preflight.affected_workers({'PREFLIGHT_TEST_WORKERS': 'x'}, cores=32, mem_gib=110) == 16
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text('MemTotal:       33554432 kB\nMemAvailable:    8388608 kB\n')
    assert preflight.mem_available_gib(str(meminfo)) == 8
    assert preflight.mem_available_gib(str(tmp_path / 'missing')) is None
