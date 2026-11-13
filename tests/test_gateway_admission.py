"""Gateway refusal happens before a route is offered or a process is launched."""
import json
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.routing_support.leg_state_flag import service_plan
from types import SimpleNamespace
from plangraph.planner.route_policy import RoutePolicy
from plangraph.routing_support import clients


@pytest.fixture
def gateway_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    for name in ('OMNIROUTE_BASE_URL', 'PLANGRAPH_GATEWAY_URL',
                 'OPENCODE_CONFIG_CONTENT', 'AUTOOS_OMNIROUTE_KEY',
                 'PLANGRAPH_GATEWAY_KEY_FILE'):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_gateway_configuration_refuses_before_route(gateway_env, monkeypatch):
    leg = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.1-sol-xhigh')
    for base, key, admitted in ((False, False, False), (True, False, False),
                                (False, True, False), (True, True, True)):
        monkeypatch.setenv('OMNIROUTE_BASE_URL', 'https://gateway.example.invalid/v1' if base else '')
        key_file = gateway_env / 'client.key'
        key_file.write_text('fixture-key')
        monkeypatch.setenv('PLANGRAPH_GATEWAY_KEY_FILE', str(key_file if key else gateway_env / 'absent'))
        plan = service_plan(SimpleNamespace(leg_reader=lambda *a, **k: {'state': 'up', 'evidence': None}), card={'estimate_tokens': 1000}, role='implement', legs=(leg,),
                          catalog_rows=[], policy=RoutePolicy())
        assert bool(plan.legs) is admitted
        if not admitted:
            assert 'gateway not configured' in json.dumps(plan.to_dict())


def test_child_and_health_share_gateway_admission(gateway_env, monkeypatch):
    assert clients.gateway_configuration_error() == 'gateway not configured: base URL missing'
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'https://gateway.example.invalid/v1')
    assert clients.gateway_configuration_error() == 'gateway not configured: client key missing'
    key = gateway_env / 'client.key'
    key.write_text('fixture-key')
    monkeypatch.setenv('PLANGRAPH_GATEWAY_KEY_FILE', str(key))
    assert clients.gateway_configuration_error() is None
