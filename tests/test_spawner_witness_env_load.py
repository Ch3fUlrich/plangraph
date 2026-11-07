import json
from types import SimpleNamespace

import pytest

from plangraph.spawner import gateway_witness as gw


@pytest.mark.parametrize('provider,served,tagged,reason', [
    ('direct', None, True, 'not a gateway leg: provider direct'),
    ('mimo', None, True, 'mimo leg not routed through gateway'),
    ('omniroute', 'other/model', True, 'served_model already set'),
    ('omniroute', None, False, 'final config carries no session tag'),
    ('omniroute', None, True, 'no gateway token in spawn env'),
    ('omniroute', None, True, 'gateway endpoint not configured'),
])
def test_unavailable(provider, served, tagged, reason):
    cfg = {'provider': {'omniroute': {'headers': {gw.SESSION_HEADER: gw.session_tag('job')}}}}
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg) if tagged else '{}'}
    job = SimpleNamespace(model_or_combo=provider + '/model', served_model=served, events=[])
    def fetch(*args):
        raise LookupError(reason)
    gw.witness(None, 'job', job, env, fetch=fetch)
    assert job.events == [{'event': 'gateway_witness_unavailable', 'reason': reason}]


def test_token_file(tmp_path):
    path = tmp_path / 'synthetic-key'
    path.write_text('synthetic-test-credential\n')
    path.chmod(0o600)
    secrets = {gw.TOKEN_FILE_ENV: str(path), 'OMNIROUTE_BASE_URL': 'http://invalid/v1'}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'[]'
    assert gw.fetch_rows('job', {}, secrets=secrets, opener=lambda *a, **k: Response()) == []
    path.chmod(0o644)
    with pytest.raises(LookupError, match='no gateway token in spawn env'):
        gw.load_token(secrets)
    path.unlink()
    with pytest.raises(LookupError, match='no gateway token in spawn env'):
        gw.load_token(secrets)


@pytest.mark.parametrize('model,reason', [
    ('direct/model', 'not a gateway leg: provider direct'),
    ('mimo/model', 'mimo leg not routed through gateway'),
    ('omniroute/model', 'no gateway token in spawn env'),
])
def test_required_refusal(monkeypatch, tmp_path, model, reason):
    from plangraph.planner.routing import SpawnRefused
    monkeypatch.delenv(gw.TOKEN_ENV, raising=False)
    monkeypatch.setenv(gw.TOKEN_FILE_ENV, str(tmp_path / 'missing'))
    with pytest.raises(SpawnRefused, match=reason):
        gw.require_witness(model, {})


def test_route_gateway_first(monkeypatch, tmp_path):
    from dataclasses import dataclass
    from plangraph.spawner.service import SpawnService
    import plangraph.routing_support.leg_state_flag as routing
    import plangraph.planner.measured_down as measured
    @dataclass
    class Plan:
        legs: tuple
    direct = SimpleNamespace(id='direct', model='direct/model')
    gateway = SimpleNamespace(id='gateway', model='omniroute/model')
    monkeypatch.setattr(routing, 'service_plan', lambda *a, **k: Plan((direct, gateway)))
    monkeypatch.setattr(measured, 'measured_down', lambda *a: set())
    service = SimpleNamespace(state=tmp_path)
    plan = SpawnService._route(service, {'witness_required': True})
    assert plan.legs == (gateway, direct)
