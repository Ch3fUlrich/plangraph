"""Synthetic boundary regressions, not live acceptance."""
import socket
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state, resolver, host_keys


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from plangraph.spawner import leg_probe
    def deny(*a, **kw):
        pytest.fail('external access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(subprocess, 'run', deny)
    monkeypatch.setattr(host_keys, '_keys_path', deny)
    monkeypatch.setattr(leg_probe, '_INFLIGHT', {})
    yield
    assert leg_probe._INFLIGHT == {}


def adapter(tmp_path, *, available=True, served=('opencode-go/sample',), enabled=True):
    calls = []
    provider = SimpleNamespace(snapshot=lambda: calls.append(1) or {
        'opencode-go': SimpleNamespace(available=available, stale=False)})
    store = leg_state.LegStateStore(tmp_path / 'state')
    reader = leg_state.LegAdapter(store, provider, lambda: served, enabled=enabled,
                                  clock=lambda: 0)
    return reader, calls


# Route order under D-6310 with an empty local evidence store; the list is the current policy order, not an origin/main recording.
MAIN_ORDER = ['opencode-go/glm-5.3-flash', 'mimo/mimo-v2.6-pro', 'opencode-go/qwen3.8-max', 'opencode-go/muse-spark-1.3-contributor', 'opencode-go/deepseek-v4.1-flash', 'Qwen3.8-Flash', 'opencode/deepseek-v4.1-flash-free', 'opencode/glm-5.3-flash-free', 'openrouter/free', 'omniroute/cx-gpt-6.2-sol-medium', 'omniroute/cx-gpt-6.2-astra', 'omniroute/cx-gpt-6-luna', 'gemini-4-hydrogen-high', 'gemini-4-hydrogen-medium', 'gemini-4-neon-medium', 'opencode/flint-40b-free', 'opencode/big-pickle', 'gpt-oss-120b-medium', 'mimo/mimo-v2.6-flash', 'claude-sonnet-5-5-medium', 'claude-opus-4-6-thinking', 'claude-opus-5-5-high', 'omniroute/deepseek-v4.1-flash', 'omniroute/spark-1.3-contributor', 'claude-opus-5-5-medium', 'opencode-go/beam-free', 'omniroute/gc/grok-4.7', 'console-meta/muse-spark-1.3-contributor', 'omniroute/deepseek-direct-flash', 'claude-haiku-5-5']


@pytest.mark.parametrize('available,expected', [(True, True), (False, False)])
def test_authority(tmp_path, available, expected):
    reader, calls = adapter(tmp_path, available=available)
    row = dict(id='opencode-go/sample', available=0, active=True, expires='2000-01-01')
    assert resolver.is_model_available(row, reader) is expected
    assert resolver.is_model_available(row, reader) is expected
    assert calls == [1]


def test_not_listed_and_failure(tmp_path):
    reader, _ = adapter(tmp_path, served=())
    assert reader('opencode-go/sample')['reason'] == 'not_listed'
    reader.models = lambda: (_ for _ in ()).throw(RuntimeError('discovery read failed'))
    reader._at = None
    assert reader('opencode-go/sample')['reason'] == 'leg_state_snapshot_error: RuntimeError: discovery read failed'


def test_off_no_reads(tmp_path):
    reader, calls = adapter(tmp_path, enabled=False)
    assert reader('opencode-go/sample') == dict(state='ok', evidence=None)
    assert calls == []


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('state', ['ok', 'limited', 'down', 'off', 'reprobe'])
def test_legacy_availability_preserves_state_policy(enabled, state):
    def reader(model, **scope):
        assert model == 'opencode-go/sample'
        assert scope == dict(account='sample', combo='worker')
        return dict(state=state, evidence=None)

    reader.enabled = enabled
    row = dict(id='opencode-go/sample', available=1, active=True)
    expected = state == 'ok' if enabled else state != 'limited'
    assert resolver.is_model_available(row, reader, account='sample', combo='worker') is expected


def test_removed_first(tmp_path):
    reader, calls = adapter(tmp_path)
    for mid in resolver.OPERATOR_REMOVED_MODELS:
        assert not resolver.is_model_available(dict(id=mid, available=1, active=True), reader)
    assert calls == []


def test_identity(tmp_path):
    reader, _ = adapter(tmp_path, served=('omniroute/cx-gpt-6.1-terra',))
    assert reader('omniroute/cx-gpt-6.1-sol')['reason'] == 'not_listed'
    assert reader.identity('agy/claude-sonnet-5-5-medium') == reader.identity('agy/claude-sonnet-5-5')


def test_disabled_policy_retained(tmp_path):
    from plangraph.planner.legs import DEFAULT_LEGS
    reader, _ = adapter(tmp_path)
    disabled = [leg for leg in DEFAULT_LEGS if leg.disabled]
    assert disabled
    for leg in disabled:
        assert not resolver.is_model_available(dict(id=leg.model, active=True,
                                                   disabled=leg.disabled), reader)


def test_stale_and_unreachable(tmp_path):
    reader, _ = adapter(tmp_path)
    reader.provider.snapshot = lambda: {'opencode-go': SimpleNamespace(available=True, stale=True)}
    assert reader('opencode-go/sample')['reason'] == 'stale'
    reader._at = None
    reader.provider.snapshot = lambda: {}
    assert reader('opencode-go/sample')['reason'] == 'unreachable'


def test_catalog_policy_is_not_mutated(tmp_path):
    import copy
    before = copy.deepcopy(resolver.DEFAULT_FALLBACK_MODELS)
    reader, calls = adapter(tmp_path, enabled=False)
    first = [m['id'] for m in before if resolver.is_model_available(m, reader)]
    second = [m['id'] for m in resolver.DEFAULT_FALLBACK_MODELS
              if resolver.is_model_available(m, reader)]
    assert first == second == MAIN_ORDER
    assert resolver.DEFAULT_FALLBACK_MODELS == before
    assert not calls


def test_bounded_discovery(monkeypatch):
    def run(argv, **kwargs):
        assert argv == ['opencode', 'models', '--standalone']
        assert kwargs['timeout'] == 8 and kwargs['shell'] is False
        return SimpleNamespace(stdout='opencode-go/sample\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == ['opencode-go/sample']


def test_discovery_child_never_inherits_parent_only_management_token(monkeypatch):
    from plangraph.routing_support.opencode_render import TOKEN_ENV, TOKEN_FILE_ENV
    monkeypatch.setenv(TOKEN_ENV, 'synthetic-management-value')
    monkeypatch.setenv(TOKEN_FILE_ENV, '/synthetic/token/file')
    monkeypatch.setenv('PATH_PROBE_KEEP', 'kept')
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout='opencode-go/sample\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == ['opencode-go/sample']
    assert TOKEN_ENV not in seen['env'] and TOKEN_FILE_ENV not in seen['env']
    assert seen['env']['PATH_PROBE_KEEP'] == 'kept'


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired('opencode', 8), OSError(),
                                  subprocess.CalledProcessError(1, 'opencode')])
def test_failed_discovery_is_a_snapshot_error_not_a_missing_model(tmp_path, monkeypatch, error):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, 'run', run)
    reader, _ = adapter(tmp_path)
    reader = leg_state.LegAdapter(reader.store, reader.provider, enabled=True)
    status = reader('opencode-go/sample')
    assert status['reason'].startswith('leg_state_snapshot_error: ')
    assert status['reason'] != 'not_listed'


def test_failed_discovery_keeps_the_probe_recovery_path(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe

    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('opencode', 8)
    monkeypatch.setattr(subprocess, 'run', run)
    reader, _ = adapter(tmp_path)
    reader = leg_state.LegAdapter(reader.store, reader.provider, enabled=True)
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')
    calls = []

    async def probe(*args, **kwargs):
        calls.append(args)
        return dict(ok=True, reason='')
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(
        SimpleNamespace(leg_reader=reader), leg, 'implement', False)) is None
    assert len(calls) == 1


def test_default_discovery_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout='opencode-go/sample\n'))
    reader, _ = adapter(tmp_path)
    reader = leg_state.LegAdapter(reader.store, reader.provider, enabled=True)
    assert reader.models() == ['opencode-go/sample']
    assert reader('opencode-go/sample')['state'] == 'ok'


@pytest.mark.parametrize('error', [subprocess.CalledProcessError(1, 'opencode'),
                                  subprocess.TimeoutExpired('opencode', 8), OSError()])
def test_bounded_discovery_failure(monkeypatch, error):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == []


def test_refresh_recovers_without_failure_becoming_health(tmp_path):
    reader, _ = adapter(tmp_path, available=False)
    assert reader('opencode-go/sample') == dict(state='down', reason='provider_inactive', evidence=None)
    reader.clock = lambda: 61
    reader.provider.snapshot = lambda: (_ for _ in ()).throw(RuntimeError())
    assert reader('opencode-go/sample')['state'] == 'unknown'
    reader.clock = lambda: 122
    reader.provider.snapshot = lambda: {'opencode-go': SimpleNamespace(available=True, stale=False)}
    assert reader('opencode-go/sample')['state'] == 'ok'


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('policy', [dict(id='cxa/gpt-6-sol-x'), dict(status='retired')])
def test_shared_admission(tmp_path, enabled, policy):
    reader, _ = adapter(tmp_path, enabled=enabled, served=('cxa/gpt-6-sol-x', 'opencode-go/sample'))
    reader.provider.snapshot = lambda: {'cxa': SimpleNamespace(available=True, stale=False),
                                      'opencode-go': SimpleNamespace(available=True, stale=False)}
    assert not resolver.is_model_available(dict(id='opencode-go/sample', active=True,
                                                available=1) | policy, reader)


def test_enabled_probe_recovery(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    reader, calls = adapter(tmp_path, available=False)
    leg = SimpleNamespace(model='opencode-go/sample', account='sample', combo='worker')
    runner = SimpleNamespace(leg_reader=reader)
    probes = []
    async def recover(*args, **kwargs):
        assert calls == [1]  # admission checks the unavailable snapshot before probing
        probes.append(1)
        reader.provider.snapshot = lambda: calls.append(1) or {'opencode-go': SimpleNamespace(available=True, stale=False)}
        return dict(ok=True)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', recover)
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
    assert probes == [1] and calls == [1, 1]


@pytest.mark.parametrize('initial_read_error', [False, True])
def test_probe_success_admits_after_snapshot_reread_error(tmp_path, monkeypatch, caplog,
                                                         initial_read_error):
    import asyncio
    from plangraph.spawner import leg_probe
    reader, _ = adapter(tmp_path, available=False)
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')
    runner = SimpleNamespace(leg_reader=reader)
    events = []

    def broken_snapshot():
        events.append('read error')
        raise OSError('snapshot unavailable')

    def initial_snapshot():
        events.append('read')
        return {'opencode-go': SimpleNamespace(available=False, stale=False)}

    reader.provider.snapshot = broken_snapshot if initial_read_error else initial_snapshot

    async def probe(actual_runner, actual_leg, **kwargs):
        assert actual_runner is runner and actual_leg is leg
        assert kwargs['store'] is reader.store
        events.append('probe')
        reader.store.record_probe_pass(leg.model, 'test')
        reader.provider.snapshot = broken_snapshot
        return dict(ok=True)

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
    assert events == (['read error', 'read error', 'probe'] if initial_read_error else
                      ['read', 'probe', 'read error', 'read error'])
    assert reader.store.last_probe_ts(leg.model) is not None
    assert 'leg_state_snapshot_error: OSError: snapshot unavailable' in caplog.text


@pytest.mark.parametrize('error', [OSError('snapshot missing'), ValueError('partial snapshot'),
                                  RuntimeError('refresh rejected')])
def test_snapshot_refresh_keeps_error(tmp_path, error):
    reader, _ = adapter(tmp_path)
    assert reader('opencode-go/sample')['state'] == 'ok'
    previous = reader._states

    def snapshot():
        raise error

    reader.provider.snapshot = snapshot
    reader.clock = lambda: 61
    assert reader('opencode-go/sample') == dict(
        state='unknown', evidence=None,
        reason=f'leg_state_snapshot_error: {type(error).__name__}: {error}')
    assert reader._states is previous  # stale health cannot mask a failed read


@pytest.fixture
def snapshot_failure(tmp_path):
    reader, _ = adapter(tmp_path)
    events = []

    def snapshot():
        events.append('read')
        raise OSError('snapshot unavailable')

    reader.provider.snapshot = snapshot
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')
    return SimpleNamespace(reader=reader, runner=SimpleNamespace(leg_reader=reader),
                           leg=leg, events=events)


@pytest.mark.parametrize('role,ttl,fresh', [('review', '0', False),
    ('implement', '0', False), ('fix', '1800', False), ('implement', '1800', True)])
@pytest.mark.parametrize('probe_ok,probe_reason,infra', [(False, 'timeout', False),
    (True, '', False), (False, 'probe infra: git', True)])
def test_snapshot_error_retries_then_probes(snapshot_failure, monkeypatch, role, ttl, fresh,
                                           probe_ok, probe_reason, infra):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure
    if fresh:
        case.reader.store.record_probe_pass(case.leg.model, 'test', now=100)
        monkeypatch.setattr(leg_probe.time, 'time', lambda: 101)

    async def probe(runner, leg, **kwargs):
        assert runner is case.runner and leg is case.leg
        assert kwargs['store'] is case.reader.store
        assert case.events == ['read', 'read']  # retry precedes any probe/verdict
        case.events.append('probe')
        return dict(ok=probe_ok, reason=probe_reason, infra=infra)

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', ttl)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    reason = asyncio.run(leg_probe.before_launch(case.runner, case.leg, role, False))
    assert case.events == ['read', 'read', 'probe']
    assert reason == (None if probe_ok else
        'leg_state_snapshot_error: OSError: snapshot unavailable; probe: ' + probe_reason)
    assert case.reader.store.last_probe_ts(case.leg.model) == (100 if fresh else None)
    # The same role/TTL/freshness inputs still control ordinary healthy admission.
    case.reader._at = None
    case.reader.provider.snapshot = lambda: {
        'opencode-go': SimpleNamespace(available=True, stale=False)}
    ordinary = []

    async def healthy_probe(*args, **kwargs):
        ordinary.append('probe')
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', healthy_probe)
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, role, False)) is None
    assert ordinary == (['probe'] if role in ('implement', 'fix') and ttl != '0' and not fresh else [])


@pytest.mark.parametrize('role', ['implement', 'fix'])
def test_not_listed_leg_never_reaches_model_run_probe(tmp_path, monkeypatch, role):
    import asyncio
    from plangraph.spawner import leg_probe
    reader, _ = adapter(tmp_path, served=())
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')
    runner = SimpleNamespace(leg_reader=reader)
    calls = []

    async def probe(*args, **kwargs):
        calls.append(args)
        return dict(ok=True, reason='')

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(runner, leg, role, False)) == 'not_listed'
    assert not calls


def test_snapshot_retry_recovers_without_probe(snapshot_failure, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure
    failed_read = case.reader.provider.snapshot

    def snapshot():
        if not case.events:
            return failed_read()
        case.events.append('read')
        return {'opencode-go': SimpleNamespace(available=True, stale=False)}

    async def probe(*args, **kwargs):
        pytest.fail('a recovered review snapshot needs no probe')

    monkeypatch.setattr(case.reader.provider, 'snapshot', snapshot)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'review', False)) is None
    assert case.events == ['read', 'read']


def test_snapshot_probe_success_cannot_clear_new_provider_limit(snapshot_failure, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure
    monkeypatch.setattr(leg_probe.time, 'time', lambda: 100)

    async def probe(*args, **kwargs):
        assert case.events == ['read', 'read']
        case.events.append('probe')
        case.reader.store.record_harness_limit(case.leg.model, 'error: quota reached',
                                                'test', now=100, down_s=60)
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'review', False)) == \
        'limited until 1970-01-01T00:02:40+00:00'
    assert case.events == ['read', 'read', 'probe']


def test_snapshot_probe_sharing_is_loop_hermetic(snapshot_failure, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def probe(*args, **kwargs):
            case.events.append('probe')
            started.set()
            await release.wait()
            return dict(ok=True)

        monkeypatch.setattr(leg_probe, 'probe', probe)
        launches = [asyncio.create_task(leg_probe.before_launch(
            case.runner, case.leg, 'review', False)) for _ in range(3)]
        await asyncio.wait_for(started.wait(), 1)
        assert len(leg_probe._INFLIGHT) == 1
        release.set()
        assert await asyncio.gather(*launches) == [None] * 3
        assert leg_probe._INFLIGHT == {}

    for _ in range(2):  # a new event loop cannot inherit a previous loop's task
        case.reader._at = None
        case.events.clear()
        asyncio.run(run())
        assert case.events.count('probe') == 1


@pytest.mark.parametrize('probe_reason', ['', None, 'unknown', ' Unknown '])
def test_snapshot_error_probe_without_reason_is_named_not_unknown(snapshot_failure, monkeypatch, probe_reason):
    import asyncio
    from plangraph.spawner import leg_probe

    async def probe(runner, leg, **kwargs):
        return dict(ok=False, reason=probe_reason, infra=False)

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '0')
    monkeypatch.setattr(leg_probe, 'probe', probe)
    case = snapshot_failure
    reason = asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'review', False))
    assert reason == 'leg_state_snapshot_error: OSError: snapshot unavailable; probe: leg_probe_failed'


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
    assert leg_state.gateway_url('omniroute/deepseek-v4.1-flash') == 'http://127.0.0.1:8080/v1'   # the launch endpoint


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
