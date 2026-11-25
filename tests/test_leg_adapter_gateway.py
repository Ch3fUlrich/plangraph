"""Gateway-prefixed legs: pool resolution, launch endpoint probes and the planner routes (synthetic, no live access)."""
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state
from test_leg_adapter import isolated  # noqa: F401  (autouse: no external access)


def gateway(tmp_path, listed, snapshot, **kw):
    provider = SimpleNamespace(snapshot=lambda: snapshot)
    store = leg_state.LegStateStore(tmp_path / 'state')
    return leg_state.LegAdapter(store, provider, lambda: listed, enabled=True, clock=lambda: 0, **kw)


UPSTREAM = {'deepseek': SimpleNamespace(available=True, stale=False, reason='', until=''),
            'opencode-go': SimpleNamespace(available=True, stale=False, reason='', until='')}

UPSTREAM_MIMO = dict(UPSTREAM, **{'xiaomi-mimo': SimpleNamespace(available=True, stale=False, reason='', until='')})


def test_gateway_prefixed_leg_resolves_its_upstream_pool(tmp_path):
    reader = gateway(tmp_path, ('omniroute/deepseek-v4.1-flash',), UPSTREAM)
    assert reader('omniroute/deepseek-v4.1-flash')['state'] == 'ok'


def test_gateway_leg_follows_the_pool_of_its_model(tmp_path):
    pools = dict(UPSTREAM, deepseek=SimpleNamespace(available=False, stale=False, reason='inactive', until=''))
    reader = gateway(tmp_path, ('omniroute/deepseek-v4.1-flash',), pools)
    assert reader('omniroute/deepseek-v4.1-flash')['reason'] == 'provider_inactive'


@pytest.mark.parametrize('model', ['omniroute/deepseek-v4.1-flash', 'omniroute/deepseek-direct-flash'])
def test_gateway_leg_never_borrows_an_unrelated_pool(tmp_path, model):
    only_go = {'opencode-go': UPSTREAM['opencode-go']}
    assert gateway(tmp_path, (model,), only_go)(model)['reason'] == 'unreachable'


def test_a_model_declaring_no_pool_falls_back_only_to_an_explicit_gateway_row(tmp_path, launch_env):
    model = 'omniroute/unknown-model-x'
    assert gateway(tmp_path, (model,), UPSTREAM)(model)['reason'] == 'unreachable'
    pools = dict(UPSTREAM, omniroute=SimpleNamespace(available=True, stale=False, reason='', until=''))
    assert gateway(tmp_path, (model,), pools)(model)['state'] == 'ok'


def test_gateway_leg_empty_snapshot_stays_unreachable(tmp_path):
    reader = gateway(tmp_path, ('omniroute/deepseek-v4.1-flash',), {})
    assert reader('omniroute/deepseek-v4.1-flash')['reason'] == 'unreachable'


def test_mimo_leg_is_served_by_its_gateway_listing(tmp_path):
    reader = gateway(tmp_path, ('omniroute/mimo-v2.6-flash',), UPSTREAM)
    assert reader('mimo/mimo-v2.6-flash')['reason'] != 'not_listed'
    assert reader('mimo/mimo-v2.6-pro')['reason'] == 'not_listed'


@pytest.mark.parametrize(('listener', 'state'), [(False, 'down'), (True, 'ok')])
def test_leg_without_a_gateway_listener_is_down_before_spawn(tmp_path, listener, state):
    seen = []
    def probe(url):
        seen.append(url)
        return listener
    reader = gateway(tmp_path, ('omniroute/mimo-v2.6-pro',), UPSTREAM_MIMO, gateway_probe=probe,
                     gateway_url=lambda leg: 'http://127.0.0.1:8080/v1' if leg.startswith('mimo/') else None)
    status = reader('mimo/mimo-v2.6-pro')
    assert status['state'] == state
    if not listener:
        assert status['reason'] == 'gateway_unreachable'
    assert seen == ['http://127.0.0.1:8080/v1']
    assert reader('omniroute/mimo-v2.6-pro')['state'] == 'ok'
    assert len(seen) == 1


@pytest.fixture
def launch_env(monkeypatch, tmp_path):
    """A process environment with no gateway override and no per-user endpoint file."""
    for name in ('OMNIROUTE_BASE_URL', 'PLANGRAPH_GATEWAY_URL', 'AUTOOS_OMNIROUTE_URL',
                 'PLANGRAPH_PROVIDERS_FILE', 'OPENCODE_CONFIG_CONTENT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    return monkeypatch


def use_providers(env, tmp_path, text):
    path = tmp_path / 'providers.json'
    path.write_text(text)
    env.setenv('PLANGRAPH_PROVIDERS_FILE', str(path))


def test_gateway_url_reads_the_provider_base_url(tmp_path, launch_env):
    use_providers(launch_env, tmp_path, '{"providers": {"mimo": {"settings": {"baseURL": "http://127.0.0.1:8080/v1"}}, "opencode": {}, "omniroute": {"settings": {"baseURL": "http://127.0.0.1:8080/v1"}}}}')
    assert leg_state.gateway_url('mimo/mimo-v2.6-pro') == 'http://127.0.0.1:8080/v1'
    assert leg_state.gateway_url('opencode/big-pickle') is None
    assert leg_state.gateway_url('claude-sonnet-5-5') is None
    assert leg_state.gateway_url('omniroute/deepseek-v4.1-flash') is None


def test_tcp_gateway_probe_refuses_a_closed_port():
    import socket as _socket
    real = _socket.create_connection
    try:
        _socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError())
        assert leg_state.tcp_gateway_probe('http://127.0.0.1:8080/v1') is False
        _socket.create_connection = lambda *a, **k: SimpleNamespace(close=lambda: None)
        assert leg_state.tcp_gateway_probe('http://127.0.0.1:8080/v1') is True
    finally:
        _socket.create_connection = real


@pytest.mark.parametrize('override', ['PLANGRAPH_GATEWAY_URL', 'OMNIROUTE_BASE_URL'])
def test_gateway_url_is_the_endpoint_the_launch_overlay_selects(launch_env, override):
    """The shipped root config names a loopback endpoint; a per-run override displaces it at launch."""
    assert leg_state.gateway_url('mimo/mimo-v2.6-pro') == 'http://127.0.0.1:8080/v1'
    launch_env.setenv(override, 'http://host-a.example:9000')
    assert leg_state.gateway_url('mimo/mimo-v2.6-pro') == 'http://host-a.example:9000/v1'


def test_route_admits_mimo_when_the_launch_endpoint_listens(tmp_path, launch_env):
    from plangraph.planner.routing import plan_spawn
    launch_env.setenv('PLANGRAPH_GATEWAY_URL', 'http://host-a.example:9000')
    seen = []
    reader = gateway(tmp_path, ('omniroute/mimo-v2.6-pro',), UPSTREAM_MIMO,
                     gateway_probe=lambda url: seen.append(url) or url.startswith('http://host-a.example:'))
    card = dict(estimate_tokens=1000, diff_ref=dict(repo=str(tmp_path), base='HEAD~1', head='HEAD'))
    plan = plan_spawn(card, role='review', writer_families=('gpt',), leg_reader=reader.leg_status)
    assert 'mimo-v2.6-pro' in [leg.id for leg in plan.legs]
    assert seen == ['http://host-a.example:9000/v1']


@pytest.mark.parametrize('text', ['{not json', '{"providers": []}', '{"providers": {"mimo": 5}}',
                                  '{"providers": {"mimo": {"settings": {"baseURL": 5}}}}',
                                  '{"providers": {"mimo": {"settings": {"baseURL": "not a url"}}}}'])
def test_gateway_url_survives_malformed_provider_config(tmp_path, launch_env, text):
    use_providers(launch_env, tmp_path, text)
    assert leg_state.gateway_url('mimo/mimo-v2.6-pro') is None


@pytest.mark.parametrize('url', ['http://a..b:80/v1', 'http://127.0.0.1:99999/v1', 'http:///v1'])
def test_tcp_gateway_probe_does_not_raise_on_a_malformed_endpoint(url):
    assert leg_state.tcp_gateway_probe(url) is False


def test_gateway_leg_with_a_known_alias_pool_does_not_borrow_another_pool(tmp_path):
    pools = {'codex': SimpleNamespace(available=False, stale=False, reason='inactive', until=''),
             'deepseek': UPSTREAM['deepseek']}
    reader = gateway(tmp_path, ('omniroute/cx-gpt-6-luna',), pools)
    assert reader('omniroute/cx-gpt-6-luna')['reason'] == 'provider_inactive'
    reader = gateway(tmp_path, ('omniroute/cx-gpt-6-luna',), {'deepseek': UPSTREAM['deepseek']})
    assert reader('omniroute/cx-gpt-6-luna')['reason'] == 'unreachable'


def test_empty_provider_list_is_down_unreachable_not_a_fallback_trigger(tmp_path):
    from plangraph.routing_support.leg_state_flag import _needs_fallback
    status = gateway(tmp_path, ('omniroute/deepseek-v4.1-flash',), {})('omniroute/deepseek-v4.1-flash')
    assert (status['state'], status['reason']) == ('down', 'unreachable')
    assert not _needs_fallback(status)


def test_valid_empty_provider_list_wins_over_a_failed_model_discovery(tmp_path):
    from plangraph.routing_support.leg_state import DiscoveryFailed
    reader = gateway(tmp_path, DiscoveryFailed('TimeoutExpired'), {})
    status = reader('omniroute/deepseek-v4.1-flash')
    assert (status['state'], status['reason']) == ('down', 'unreachable')


def test_gateway_url_reads_an_options_base_url_like_the_renderer(tmp_path, launch_env):
    use_providers(launch_env, tmp_path, '{"providers": {"mimo": {"options": {"baseURL": "http://127.0.0.1:8081"}}}}')
    assert leg_state.gateway_url('mimo/mimo-v2.6-flash') == 'http://127.0.0.1:8081/v1'


def route_card(tmp_path):
    return dict(estimate_tokens=1000, diff_ref=dict(repo=str(tmp_path), base='HEAD~1', head='HEAD'))


def test_route_skips_mimo_flash_whose_options_endpoint_has_no_listener(tmp_path, launch_env):
    from plangraph.planner.routing import plan_spawn
    use_providers(launch_env, tmp_path, '{"providers": {"mimo": {"options": {"baseURL": "http://127.0.0.1:8081"}}}}')
    seen = []
    reader = gateway(tmp_path, ('omniroute/mimo-v2.6-flash',), UPSTREAM, gateway_probe=lambda url: seen.append(url) or False)
    plan = plan_spawn(route_card(tmp_path), role='review', writer_families=('gpt',), leg_reader=reader.leg_status)
    assert seen == ['http://127.0.0.1:8081/v1']
    assert {s['leg']: s['reason'] for s in plan.skipped}.get('mimo-v2.6-flash') == 'gateway_unreachable'


def omniroute_state(*pools, inactive=()):
    """The production /api/providers reader fed JSON through its injected transport."""
    import json
    from datetime import datetime, timezone
    from plangraph.routing_support.quota_snapshots import OmniRouteState
    rows = [dict(provider=name, isActive=name not in inactive) for name in pools]
    return OmniRouteState('injected-endpoint', key='synthetic', ttl_s=10, clock=lambda: 0,
                          wall_clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
                          fetch=lambda *args: (200, json.dumps({'connections': rows})))


def json_reader(tmp_path, listed, *pools, inactive=(), **kw):
    store = leg_state.LegStateStore(tmp_path / 'state')
    return leg_state.LegAdapter(store, omniroute_state(*pools, inactive=inactive), lambda: listed,
                                enabled=True, clock=lambda: 0, **kw)


def review_plan(tmp_path, reader, writers=('claude',)):
    from plangraph.planner.routing import plan_spawn
    return plan_spawn(route_card(tmp_path), role='review', writer_families=writers, leg_reader=reader.leg_status)


def test_claude_writer_route_serves_both_gateway_models_from_provider_json(tmp_path, launch_env):
    listed = ('omniroute/deepseek-v4.1-flash', 'omniroute/mimo-v2.6-flash')
    plan = review_plan(tmp_path, json_reader(tmp_path, listed, 'deepseek', 'opencode-go', 'xiaomi-mimo'))
    ids = [leg.id for leg in plan.legs]
    assert 'omniroute-deepseek-flash' in ids and 'mimo-v2.6-flash' in ids
    # Only the gateway's opencode-go pool answering admits neither DeepSeek nor MiMo.
    plan = review_plan(tmp_path, json_reader(tmp_path, listed, 'opencode-go'))
    reasons = {s['leg']: s['reason'] for s in plan.skipped}
    assert reasons.get('omniroute-deepseek-flash') == 'unreachable' and reasons.get('mimo-v2.6-flash') == 'unreachable'


def test_gateway_leg_resolves_the_pool_the_deployed_combo_declares(tmp_path, launch_env):
    spark = 'omniroute/spark-1.3-contributor'   # deploy/omniroute/live/server-combos.json: meta-api
    assert json_reader(tmp_path, (spark,), 'meta-api')(spark)['state'] == 'ok'
    assert json_reader(tmp_path, (spark,), 'meta-api', inactive=('meta-api',))(spark)['reason'] == 'provider_inactive'
    assert json_reader(tmp_path, (spark,), 'opencode-go')(spark)['reason'] == 'unreachable'
    direct = 'omniroute/deepseek-direct-flash'   # providers.json modelID deepseek/deepseek-flash
    assert json_reader(tmp_path, (direct,), 'deepseek')(direct)['state'] == 'ok'


@pytest.mark.parametrize('model', ['omniroute/deepseek-v4.1-flash', 'omniroute/deepseek-direct-flash'])
def test_the_models_own_pool_outranks_the_generic_gateway_row(tmp_path, launch_env, model):
    reader = json_reader(tmp_path, (model,), 'omniroute', 'deepseek', inactive=('deepseek',))
    assert reader(model)['reason'] == 'provider_inactive'
    # The declared pool has no row: the generic gateway row does not stand in for it.
    assert json_reader(tmp_path, (model,), 'omniroute', 'opencode-go')(model)['reason'] == 'unreachable'


@pytest.mark.parametrize('text', ['{"providers": {"mimo": 5}}', '{"providers": {"mimo": []}}'])
def test_route_survives_a_non_object_mimo_provider_with_a_gateway_override(tmp_path, launch_env, text):
    use_providers(launch_env, tmp_path, text)
    launch_env.setenv('PLANGRAPH_GATEWAY_URL', 'http://host-a.example:9000')
    plan = review_plan(tmp_path, json_reader(tmp_path, ('omniroute/mimo-v2.6-pro',), 'xiaomi-mimo',
                                             gateway_probe=lambda url: True), writers=('gpt',))
    assert plan.legs


def spark_providers(tmp_path, launch_env, pool):
    use_providers(launch_env, tmp_path, '{"providers": {"omniroute": {"models": {"spark-1.3-contributor": '
                  '{"modelID": "%s/spark"}}}}}' % pool)


def test_launch_mapping_outranks_the_server_combo_pool(tmp_path, launch_env):
    spark = 'omniroute/spark-1.3-contributor'   # the deployed combo declares meta-api
    spark_providers(tmp_path, launch_env, 'opencode-go')
    reader = json_reader(tmp_path, (spark,), 'meta-api', 'opencode-go', inactive=('opencode-go',))
    assert reader(spark)['reason'] == 'provider_inactive'


def test_an_inferred_pool_never_overrides_a_declared_pool(tmp_path):
    model = 'omniroute/deepseek-v4.1-flash'
    reader = json_reader(tmp_path, (model,), 'deepseek', 'deepseek-alt', inactive=('deepseek',))
    assert reader(model)['reason'] == 'provider_inactive'
    # Provider matching is exact: a similarly named pool never stands in.
    assert json_reader(tmp_path, (model,), 'deepseek-alt')(model)['reason'] == 'unreachable'


@pytest.mark.parametrize('text', ['{"providers": {"omniroute": 5}}', '{"providers": {"omniroute": []}}',
                                  '{"providers": {"omniroute": {"models": 5}}}',
                                  '{"providers": {"omniroute": {"models": {"deepseek-v4.1-flash": 5}}}}'])
def test_claude_writer_route_survives_a_malformed_omniroute_provider_block(tmp_path, launch_env, text):
    use_providers(launch_env, tmp_path, text)
    plan = review_plan(tmp_path, json_reader(tmp_path, ('omniroute/deepseek-v4.1-flash',), 'deepseek'))
    assert 'omniroute-deepseek-flash' in [leg.id for leg in plan.legs]


def test_provider_config_replaced_in_place_refreshes_the_pool_mapping(tmp_path, launch_env):
    spark = 'omniroute/spark-1.3-contributor'
    spark_providers(tmp_path, launch_env, 'opencode-go')
    reader = json_reader(tmp_path, (spark,), 'opencode-go', 'meta-api', 'other-pool', inactive=('other-pool',))
    assert reader(spark)['state'] == 'ok'
    spark_providers(tmp_path, launch_env, 'other-pool')   # same path, new content
    reader._at = None
    assert reader(spark)['reason'] == 'provider_inactive'


def deepseek_providers(tmp_path, launch_env, pool):
    use_providers(launch_env, tmp_path, '{"providers": {"omniroute": {"models": {"deepseek-v4.1-flash": '
                  '{"modelID": "%s/deepseek-v4.1-flash"}}}}}' % pool)


def test_a_direct_mapping_is_authoritative_when_its_pool_is_absent(tmp_path, launch_env):
    model = 'omniroute/deepseek-v4.1-flash'
    deepseek_providers(tmp_path, launch_env, 'opencode-go')   # the launch targets opencode-go
    assert json_reader(tmp_path, (model,), 'deepseek')(model)['reason'] == 'unreachable'
    plan = review_plan(tmp_path, json_reader(tmp_path, (model,), 'deepseek'))
    assert {s['leg']: s['reason'] for s in plan.skipped}.get('omniroute-deepseek-flash') == 'unreachable'
    assert json_reader(tmp_path, (model,), 'deepseek', 'opencode-go')(model)['state'] == 'ok'


def test_a_child_config_mapping_decides_like_the_launch_config(tmp_path, launch_env):
    import json
    model = 'omniroute/deepseek-v4.1-flash'
    use_providers(launch_env, tmp_path, '{"providers": {"omniroute": {}}}')   # the root config declares no mapping
    launch_env.setenv('OPENCODE_CONFIG_CONTENT', json.dumps({'providers': {'omniroute': {'models': {
        'deepseek-v4.1-flash': {'modelID': 'opencode-go/deepseek-v4.1-flash'}}}}}))
    reader = json_reader(tmp_path, (model,), 'deepseek', 'opencode-go', inactive=('opencode-go',))
    assert reader(model)['reason'] == 'provider_inactive'
    plan = review_plan(tmp_path, json_reader(tmp_path, (model,), 'deepseek', 'opencode-go', inactive=('opencode-go',)))
    assert {s['leg']: s['reason'] for s in plan.skipped}.get('omniroute-deepseek-flash') == 'provider_inactive'


def test_a_replacement_that_keeps_size_and_mtime_is_read_again(tmp_path, launch_env):
    import os
    model = 'omniroute/deepseek-v4.1-flash'
    deepseek_providers(tmp_path, launch_env, 'opencode-go')
    path = tmp_path / 'providers.json'
    reader = json_reader(tmp_path, (model,), 'opencode-go', 'pool-two', inactive=('pool-two',))
    assert reader(model)['state'] == 'ok'
    before = path.stat()
    twin = tmp_path / 'twin.json'
    twin.write_text(path.read_text().replace('opencode-go', 'pool-two'.ljust(len('opencode-go'), 'x')))
    os.utime(twin, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert twin.stat().st_size == before.st_size
    os.replace(twin, path)
    reader._at = None
    # The replacement maps the model to a pool that has no row: the old active mapping must not linger.
    assert reader(model)['reason'] == 'unreachable'


@pytest.mark.parametrize('other', ['proxy-deepseek', 'deepseek-alt'])
def test_provider_match_is_exact_not_a_suffix_or_prefix(tmp_path, other):
    model = 'omniroute/deepseek-v4.1-flash'
    assert json_reader(tmp_path, (model,), 'deepseek', other, inactive=('deepseek',))(model)['reason'] == 'provider_inactive'
    plan = review_plan(tmp_path, json_reader(tmp_path, (model,), other))
    assert {s['leg']: s['reason'] for s in plan.skipped}.get('omniroute-deepseek-flash') == 'unreachable'


@pytest.mark.parametrize(('pools', 'expected'), [(('omniroute',), 'unreachable'), (('omniroute', 'mimo'), 'ok'),
                                                 (('omniroute', 'xiaomi-mimo'), 'ok')])
def test_the_renderer_fallback_mimo_provider_decides_mimo_flash(tmp_path, launch_env, pools, expected):
    use_providers(launch_env, tmp_path, '{"providers": {}}')   # the renderer adds the mimo provider
    plan = review_plan(tmp_path, json_reader(tmp_path, ('omniroute/mimo-v2.6-flash',), *pools), writers=('gpt',))
    served = 'mimo-v2.6-flash' in [leg.id for leg in plan.legs]
    assert served == (expected == 'ok')
    if not served:
        assert {s['leg']: s['reason'] for s in plan.skipped}.get('mimo-v2.6-flash') == 'unreachable'


def test_the_effective_model_id_names_the_combo(tmp_path, launch_env):
    model = 'omniroute/deepseek-v4.1-flash'
    use_providers(launch_env, tmp_path, '{"providers": {"omniroute": {"models": {"deepseek-v4.1-flash": '
                  '{"modelID": "spark-1.3-contributor"}}}}}')   # the launch runs the Spark combo (meta-api)
    reader = json_reader(tmp_path, (model,), 'deepseek', 'meta-api', inactive=('meta-api',))
    assert reader(model)['reason'] == 'provider_inactive'
    assert json_reader(tmp_path, (model,), 'deepseek')(model)['reason'] == 'unreachable'


GROK = 'omniroute/gc/grok-4.7'


@pytest.mark.parametrize('model_id', ['xai/grok-4.7', 'spark-1.3-contributor'])
def test_the_launch_target_outranks_a_known_alias(tmp_path, launch_env, model_id):
    use_providers(launch_env, tmp_path, '{"providers": {"omniroute": {"models": {"gc/grok-4.7": '
                  '{"modelID": "%s"}}}}}' % model_id)
    pools = ('grok-cli', 'xai', 'meta-api')
    inactive = ('xai', 'meta-api')   # the alias pool is active, the launch target is not
    assert json_reader(tmp_path, (GROK,), *pools, inactive=inactive)(GROK)['reason'] == 'provider_inactive'


def test_a_known_alias_decides_a_leg_without_a_declared_pool(tmp_path, launch_env):
    use_providers(launch_env, tmp_path, '{"providers": {}}')
    assert json_reader(tmp_path, (GROK,), 'grok-cli', 'xai')(GROK)['state'] == 'ok'
    assert json_reader(tmp_path, (GROK,), 'grok-cli', 'xai', inactive=('grok-cli',))(GROK)['reason'] == 'provider_inactive'
