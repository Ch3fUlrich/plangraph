import json
from types import SimpleNamespace

import pytest

from plangraph.spawner import gateway_witness as gw


@pytest.mark.parametrize('provider,served,tagged,reason', [
    ('direct', None, True, 'not a gateway leg: provider direct'),
    ('mimo', None, True, 'gateway endpoint not configured'),
    ('omniroute', 'other/model', True, 'served_model already set'),
    ('omniroute', None, False, 'final config carries no session tag'),
    ('omniroute', None, True, 'no ' + gw.TOKEN_ENV),
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
    with pytest.raises(LookupError, match='no ' + gw.TOKEN_ENV):
        gw.load_token(secrets)
    path.unlink()
    with pytest.raises(LookupError, match='no ' + gw.TOKEN_ENV):
        gw.load_token(secrets)


@pytest.mark.parametrize('model,reason', [
    ('direct/model', 'not a gateway leg: provider direct'),
    ('mimo/model', 'mimo leg not routed through gateway'),
    ('omniroute/model', 'no ' + gw.TOKEN_ENV),
])
def test_required_refusal(monkeypatch, tmp_path, model, reason):
    from plangraph.planner.routing import SpawnRefused
    monkeypatch.delenv(gw.TOKEN_ENV, raising=False)
    monkeypatch.setenv(gw.TOKEN_FILE_ENV, str(tmp_path / 'missing'))
    with pytest.raises(SpawnRefused, match=reason):
        gw.require_witness(model, {}, secrets={gw.TOKEN_FILE_ENV: str(tmp_path / 'missing')})


def test_route_gateway_first(monkeypatch, tmp_path):
    from dataclasses import dataclass
    from plangraph.spawner.service import SpawnService
    import plangraph.routing_support.leg_state_flag as routing
    import plangraph.planner.measured_down as measured
    @dataclass
    class Plan:
        legs: tuple
        review_seats: tuple = ()
    direct = SimpleNamespace(id='direct', model='direct/model', disabled=None)
    gateway = SimpleNamespace(id='gateway', model='omniroute/model', disabled=None)
    monkeypatch.setattr(routing, 'service_plan', lambda *a, **k: Plan((direct, gateway)))
    monkeypatch.setattr(measured, 'measured_down', lambda *a: set())
    service = SimpleNamespace(state=tmp_path)
    plan = SpawnService._route(service, {'witness_required': True}, role='review', writer_families=['writer'])
    assert plan.legs == (gateway, direct)


def test_required_uses_pinned_secrets(monkeypatch):
    monkeypatch.setenv(gw.TOKEN_ENV, 'ambient')
    secrets = {gw.TOKEN_ENV: 'pinned', 'OMNIROUTE_BASE_URL': 'http://gateway/v1'}
    gw.require_witness('omniroute/model', secrets, secrets=secrets)
    from plangraph.planner.routing import SpawnRefused
    with pytest.raises(SpawnRefused, match='gateway endpoint not configured'):
        gw.require_witness('omniroute/model', {}, secrets={gw.TOKEN_ENV: 'pinned'})


@pytest.mark.parametrize('model', [None, 'direct/model', 'omniroute/model', 'mimo/model'])
def test_gateway_predicate_matches_requirement(model):
    from plangraph.planner.routing import SpawnRefused
    env = {'OMNIROUTE_BASE_URL': 'http://gateway/v1',
           'OPENCODE_CONFIG_CONTENT': json.dumps({'providers': {'mimo': {'settings': {}}}})}
    secrets = {gw.TOKEN_ENV: 'synthetic', 'OMNIROUTE_BASE_URL': 'http://gateway/v1'}
    if gw.gateway_leg(model, env):
        gw.require_witness(model, env, secrets=secrets)
    else:
        with pytest.raises(SpawnRefused, match='not a gateway leg'):
            gw.require_witness(model, env, secrets=secrets)


def test_gate_uses_leg_env_and_skips_other_roles():
    from plangraph.planner.routing import SpawnRefused
    secrets = {gw.TOKEN_ENV: 'synthetic', 'OMNIROUTE_BASE_URL': 'http://gateway/v1'}
    card = {'witness_required': True}
    gw.witness_gate(None, 'review', None, {}, secrets=secrets)
    gw.witness_gate(card, 'implement', None, {}, secrets=secrets)
    with pytest.raises(SpawnRefused, match='mimo leg not routed through gateway'):
        gw.witness_gate(card, 'review', 'mimo/model', {}, secrets=secrets)
    env = dict(secrets, OPENCODE_CONFIG_CONTENT=json.dumps(
        {'providers': {'mimo': {'settings': {'baseURL': 'http://gateway/v1'}}}}))
    gw.witness_gate(card, 'review', 'mimo/model', env, secrets=secrets)


def test_required_plan_refused_before_run_artifacts(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan, SpawnRefused
    from plangraph.spawner.service import SpawnService
    gateway = replace(DEFAULT_LEGS[0], model='omniroute/model')
    direct = replace(gateway, id='direct', model='direct/model')
    plan = SpawnPlan((direct,), (), 'test', (), ())
    monkeypatch.setenv(gw.TOKEN_ENV, 'synthetic')
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'http://gateway/v1')
    service = SpawnService(state=tmp_path)
    monkeypatch.setattr(service, '_annotate_spawn', lambda *a: pytest.fail('annotation before gate'))
    with pytest.raises(SpawnRefused, match='not a gateway leg'):
        asyncio.run(service._spawn(prompt='review', card={'witness_required': True},
                                  role='review', _plan=plan))
    assert not list(tmp_path.rglob('live.log'))
    assert not list(tmp_path.rglob('run-*/run.json'))


def test_mixed_gate_retains_all_capable_legs(monkeypatch):
    monkeypatch.setenv(gw.TOKEN_ENV, 'synthetic')
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'http://gateway/v1')
    env = {'OMNIROUTE_BASE_URL': 'http://gateway/v1',
           'OPENCODE_CONFIG_CONTENT': json.dumps({'provider': {'mimo': {
               'options': {'baseURL': 'http://gateway/v1'}}}})}
    monkeypatch.setattr(gw, 'review_child_env', lambda: env)
    direct, mimo, gateway = [SimpleNamespace(id=p, model=p + '/model', disabled=None)
                             for p in ('direct', 'mimo', 'omniroute')]
    legs = (direct, mimo, gateway)
    card = {'witness_required': True}
    assert gw.witness_order(card, 'review', legs, set()) == (mimo, gateway, direct)
    assert gw.witness_gate_legs(card, 'review', legs) == (mimo, gateway)
    # A later native witness implementation needs to widen only this predicate.
    monkeypatch.setattr(gw, 'gateway_leg', lambda model, env=None: True)
    assert gw.witness_gate_legs(card, 'review', legs) == legs
    assert gw.witness_order(card, 'review', legs, set()) == legs


@pytest.mark.parametrize('card,role', [(None, 'review'), ({}, 'review'),
    ({'witness_required': True}, 'implement')])
def test_nonrequired_never_builds_env(monkeypatch, card, role):
    monkeypatch.setattr(gw, 'review_child_env', lambda: pytest.fail('unneeded child env'))
    legs = (SimpleNamespace(id='direct', model='direct/model', disabled=None),)
    assert gw.witness_gate_legs(card, role, legs) == legs
    assert gw.witness_order(card, role, legs, set()) == legs


def test_child_endpoint_must_match_witness_endpoint():
    from plangraph.planner.routing import SpawnRefused
    secrets = {gw.TOKEN_ENV: 'synthetic', 'OMNIROUTE_BASE_URL': 'http://gateway/v1'}
    with pytest.raises(SpawnRefused, match='differs from witness endpoint'):
        gw.require_witness('omniroute/model', {'OMNIROUTE_BASE_URL': 'http://direct/v1'},
                           secrets=secrets)


def test_review_env_uses_runner_envelope(monkeypatch):
    from plangraph.routing_support import clients
    captured = {}
    def build(extra, *, headers):
        captured.update(extra=extra, headers=headers)
        return {'rendered': 'yes'}
    monkeypatch.setattr(clients, 'build_child_env', build)
    assert gw.review_child_env() == {'rendered': 'yes'}
    assert captured == {'extra': {}, 'headers': clients.build_routing_envelope_headers(
        role='review', risk='normal', min_intelligence=44.0, required_context=32000)}
