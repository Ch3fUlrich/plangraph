import pytest

from plangraph.routing_support.leg_state import LegStateStore


def test_down_expiry_and_recovery(tmp_path):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    assert store.last_probe_ts('agy/model') is None
    store.record_probe_fail('agy/model', 'timeout', 'leg-probe', now=100, down_s=20)
    state = store.leg_status('agy/model', now=110)
    assert state['state'] == 'down'
    assert state['evidence']['body_excerpt'] == 'timeout'
    assert store.last_probe_ts('agy/model') == 100
    assert store.leg_status('agy/model', now=120)['state'] == 'reprobe'
    store.record_probe_pass('agy/model', 'leg-probe', now=121)
    assert store.leg_status('agy/model', now=121)['state'] == 'ok'
    assert store.last_probe_ts('agy/model') == 121


def test_probe_module():
    from plangraph.spawner.leg_probe import probe
    assert callable(probe)
