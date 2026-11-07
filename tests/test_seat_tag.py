"""Hermetic response-side seat boundary regressions; no live acceptance claim."""
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner import gateway_witness as gw

FETCH_ROWS = gw.fetch_rows


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(gw.os, 'environ', {})
    monkeypatch.setattr(gw, 'fetch_rows', lambda *a: pytest.fail('transport not injected'))
    monkeypatch.setattr(gw.time, 'sleep', lambda _: None)


@pytest.mark.parametrize('spelling', ['provider', 'providers'])
def test_tags_preserve_headers(spelling):
    cfg = {spelling: {p: {'headers': {'keep': 'yes'}, 'options': {
        'headers': {'also': 'yes'}}} for p in ('mimo', 'omniroute', 'other')}}
    gw.tag_session(cfg, 'seat')
    for p in ('mimo', 'omniroute'):
        assert cfg[spelling][p]['headers'] == {'keep': 'yes', gw.SESSION_HEADER: 'plangraph-seat'}
        assert cfg[spelling][p]['options']['headers'] == {'also': 'yes', gw.SESSION_HEADER: 'plangraph-seat'}
    assert gw.SESSION_HEADER not in cfg[spelling]['other']['headers']


def config(endpoint='/gateway/v1'):
    cfg = {'providers': {'mimo': {'settings': {'baseURL': endpoint}}, 'omniroute': {}}}
    gw.tag_session(cfg, 'seat')
    return {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg), 'OMNIROUTE_BASE_URL': '/gateway/v1'}


def job(model='mimo/mimo-v2.6-pro'):
    return SimpleNamespace(model_or_combo=model, served_model=None, events=[], witness_kind=None)


def test_two_families_through_trusted_adapter(tmp_path):
    from plangraph.spawner.runner import SpawnerRunner
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    for model, family in [('mimo/mimo-v2.6-pro', 'mimo'), ('cx/gpt-6-luna', 'gpt')]:
        seat = job(model if family == 'mimo' else 'omniroute/' + model)
        runner.jobs['seat'] = seat
        runner._adapter_tokens['seat'] = object()
        rows = [{'sessionTag': gw.session_tag('seat'), 'status': 200, 'model': model},
                {'sessionTag': 'unrelated', 'status': 200, 'model': 'other'},
                {'sessionTag': gw.session_tag('seat'), 'status': 503, 'model': 'other'}]
        gw.witness(runner, 'seat', seat, config(), fetch=lambda *a: rows)
        assert seat.served_model == model and seat.served_family == family
        assert seat.witness_kind == gw.WITNESS_KIND


@pytest.mark.parametrize('rows', [[], [
    {'sessionTag': 'plangraph-seat', 'status': 200, 'model': 'mimo/a'},
    {'sessionTag': 'plangraph-seat', 'status': 200, 'model': 'cx/b'}]])
def test_absent_or_ambiguous_is_unwitnessed(rows):
    seat = job()
    gw.witness(SimpleNamespace(), 'seat', seat, config(), fetch=lambda *a: rows)
    assert seat.served_model is None and seat.events


def test_direct_mimo_is_not_gateway_backed():
    seat = job()
    gw.witness(SimpleNamespace(), 'seat', seat, config('/direct/v1'), fetch=pytest.fail)
    assert seat.served_model is None and not seat.events


def test_missing_management_access():
    seat = job()
    gw.witness(SimpleNamespace(), 'seat', seat, config(),
               fetch=lambda tag, env: FETCH_ROWS(tag, env, secrets={}, opener=pytest.fail))
    assert seat.events == [{'event': 'gateway_witness_unavailable', 'reason': 'no ' + gw.TOKEN_ENV}]


def test_no_endpoint_has_no_deployment_fallback():
    with pytest.raises(LookupError, match='endpoint'):
        FETCH_ROWS('plangraph-seat', {}, secrets={gw.TOKEN_ENV: 'synthetic'}, opener=pytest.fail)


def test_tag_must_be_in_active_provider():
    assert not gw.tagged(json.dumps({'note': gw.session_tag('seat'),
                                   gw.SESSION_HEADER: 'elsewhere'}), 'seat')


def test_gateway_endpoint_normalizer_is_named_function():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gw._gateway_mimo))
    assert any(isinstance(node, ast.FunctionDef) and node.name == 'normalize'
               for node in tree.body[0].body)


@pytest.mark.parametrize('spelling', ['provider', 'providers'])
def test_final_runner_config_and_mimo_witness(tmp_path, monkeypatch, spelling):
    import asyncio
    from fake_pid import FAKE_PID
    from plangraph.spawner import resolver
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.routing_support import host_keys, opencode_render

    async def probe(*a, **kw):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    monkeypatch.setattr(host_keys, 'add_host_keys', lambda *a, **kw: None)
    # Replace any earlier tags with an untagged final provider merge.
    def render(env, model):
        cfg = {spelling: {
            name: {'headers': {'keep': 'yes'}, 'options': {'headers': {'also': 'yes'}}}
            for name in ('mimo', 'omniroute', 'other')}}
        cfg[spelling]['mimo']['settings'] = {'baseURL': '/gateway/v1'}
        assert not gw.tagged(json.dumps(cfg), 'seat')
        env.update(OPENCODE_CONFIG_CONTENT=json.dumps(cfg), OMNIROUTE_BASE_URL='/gateway/v1')
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', render)
    seen = {}

    async def factory(*a, **kw):
        seen.update(kw['env'])
        async def communicate():
            return b'', b''
        return SimpleNamespace(pid=FAKE_PID, returncode=0, communicate=communicate)

    def fetch(tag, env):
        return [{'sessionTag': tag, 'status': 200, 'model': 'mimo/mimo-v2.6-pro'}]
    monkeypatch.setattr(gw, 'fetch_rows', fetch)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        seat = await runner.spawn('synthetic', model_or_combo='mimo/mimo-v2.6-pro',
                                  role='review', client='opencode', cwd=str(tmp_path),
                                  wait_seconds=3)
        await runner._tasks[seat.job_id]
        return seat
    seat = asyncio.run(run())
    assert gw.tagged(seen['OPENCODE_CONFIG_CONTENT'], seat.job_id, 'mimo')
    providers = json.loads(seen['OPENCODE_CONFIG_CONTENT'])[spelling]
    for name in ('mimo', 'omniroute'):
        assert providers[name]['headers'] == {'keep': 'yes', gw.SESSION_HEADER: gw.session_tag(seat.job_id)}
        assert providers[name]['options']['headers'] == {'also': 'yes', gw.SESSION_HEADER: gw.session_tag(seat.job_id)}
    assert providers['other'] == {'headers': {'keep': 'yes'}, 'options': {'headers': {'also': 'yes'}}}
    assert seat.served_model == 'mimo/mimo-v2.6-pro'
    assert seat.witness_kind == gw.WITNESS_KIND
    assert gw.TOKEN_ENV not in seen


def test_mimo_without_baseurl_is_gateway_backed_and_alias_is_mapped():
    cfg = json.dumps({'provider': {'mimo': {'options': {}}}})
    assert gw._gateway_mimo({'OMNIROUTE_BASE_URL': '/gateway/v1', 'OPENCODE_CONFIG_CONTENT': cfg})
    assert not gw._gateway_mimo({'OPENCODE_CONFIG_CONTENT': cfg})
    gate = {'OMNIROUTE_BASE_URL': '/gateway/v1'}
    assert not gw._gateway_mimo(dict(gate))
    assert not gw._gateway_mimo(dict(gate, OPENCODE_CONFIG_CONTENT='not json'))
    assert not gw._gateway_mimo(dict(gate, OPENCODE_CONFIG_CONTENT=json.dumps({'provider': {'omniroute': {}}})))
    direct = json.dumps({'provider': {'mimo': {'options': {'baseURL': '/direct/v1'}}}})
    assert not gw._gateway_mimo(dict(gate, OPENCODE_CONFIG_CONTENT=direct))
    off = json.dumps({'provider': {'mimo': {'disabled': True}}})
    assert not gw._gateway_mimo(dict(gate, OPENCODE_CONFIG_CONTENT=off))
    both = json.dumps({'provider': {'mimo': {'options': {}}}, 'providers': {'mimo': {'settings': {'baseURL': '/direct/v1'}}}})
    assert not gw._gateway_mimo(dict(gate, OPENCODE_CONFIG_CONTENT=both))


def test_xiaomi_alias_with_bare_and_prefixed_model():
    rows = [{'sessionTag': 't', 'status': 200, 'provider': 'xiaomi-mimo', 'model': 'mimo-v2.6-pro'},
            {'sessionTag': 't', 'status': 200, 'provider': 'xiaomi-mimo', 'model': 'xiaomi-mimo/mimo-v2.6-pro'}]
    assert gw.served_models(rows, 't') == {'mimo/mimo-v2.6-pro'}


def test_no_baseurl_mimo_without_a_tagged_row_stays_unwitnessed():
    cfg = {'provider': {'mimo': {'options': {}}}}
    gw.tag_session(cfg, 'seat')
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg), 'OMNIROUTE_BASE_URL': '/gateway/v1'}
    seat = job()
    gw.witness(SimpleNamespace(), 'seat', seat, env, fetch=lambda *a: [])
    assert seat.served_model is None and seat.events


@pytest.mark.parametrize('decoy', ['provider', 'providers'])
@pytest.mark.parametrize('failure', ['untagged', 'direct'])
def test_both_maps_require_every_active_mimo_block(decoy, failure):
    cfg = {key: {'mimo': {'settings': {'baseURL': '/gateway/v1'}}}
           for key in ('provider', 'providers')}
    gw.tag_session(cfg, 'seat')
    block = cfg[decoy]['mimo']
    if failure == 'untagged':
        del block['headers'][gw.SESSION_HEADER]
        del block['options']['headers'][gw.SESSION_HEADER]
    else:
        block['settings']['baseURL'] = '/direct/v1'
    env = {'OMNIROUTE_BASE_URL': '/gateway/v1', 'OPENCODE_CONFIG_CONTENT': json.dumps(cfg)}
    seat = job()
    gw.witness(SimpleNamespace(), 'seat', seat, env, fetch=pytest.fail)
    assert seat.served_model is None
    if failure == 'untagged':
        assert not gw.tagged(env['OPENCODE_CONFIG_CONTENT'], 'seat', 'mimo')
        assert seat.events
    else:
        assert not gw._gateway_mimo(env)


def test_jsonc_comments_and_trailing_commas_are_witnessed(tmp_path):
    from plangraph.spawner.runner import SpawnerRunner
    content = '''{
        // The URL's slashes are not comments.
        "providers": {"mimo": {
            "settings": {"baseURL": "https://gateway.example/v1",},
            /* provider-level session header */
            "headers": {"x-omniroute-session-id": "plangraph-seat",},
        },},
    }'''
    env = {'OMNIROUTE_BASE_URL': 'https://gateway.example/v1', 'OPENCODE_CONFIG_CONTENT': content}
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    seat = job()
    runner.jobs['seat'] = seat
    runner._adapter_tokens['seat'] = object()
    assert gw.tagged(content, 'seat', 'mimo')
    assert gw._gateway_mimo(env)
    gw.witness(runner, 'seat', seat, env, fetch=lambda *a: [
        {'sessionTag': 'plangraph-seat', 'status': 200, 'model': 'mimo/mimo-v2.6-pro'}])
    assert seat.served_model == 'mimo/mimo-v2.6-pro'
    assert seat.witness_kind == gw.WITNESS_KIND


def test_real_renderer_mimo_is_tagged_and_recognised(tmp_path):
    from plangraph.routing_support.opencode_render import render_gateway_providers
    from plangraph.spawner.runner import SpawnerRunner
    env = {'OPENCODE_CONFIG_CONTENT': '{}', 'OMNIROUTE_BASE_URL': 'https://gateway.example/'}
    assert render_gateway_providers(env, 'mimo/mimo-v2.6-pro') is None
    cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert cfg['providers']['mimo']['settings']['baseURL'] == 'https://gateway.example/v1'
    gw.tag_session(cfg, 'seat')
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(cfg)
    assert gw.tagged(env['OPENCODE_CONFIG_CONTENT'], 'seat', 'mimo')
    assert gw._gateway_mimo(env)
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    seat = job()
    runner.jobs['seat'] = seat
    runner._adapter_tokens['seat'] = object()
    gw.witness(runner, 'seat', seat, env, fetch=lambda *a: [
        {'sessionTag': 'plangraph-seat', 'status': 200, 'model': 'mimo/mimo-v2.6-pro'}])
    assert seat.served_model == 'mimo/mimo-v2.6-pro'
    assert seat.witness_kind == gw.WITNESS_KIND


def test_gateway_normalizes_before_fallback():
    assert gw._gateway({'OMNIROUTE_BASE_URL': ' / ', 'PLANGRAPH_GATEWAY_URL': ' https://gw/v1/ '}) == 'https://gw/v1'
