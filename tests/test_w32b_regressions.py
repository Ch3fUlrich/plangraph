import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from plangraph.spawner import resolver as r, opencode_overlay as o
from plangraph.spawner.clients import build_command, build_standalone_fallback_command


@pytest.mark.parametrize('text', [
    '{"providers": {} /* note {"key": "v"} */, "x": 1}',
    '{/* {" */ "providers": {}}',
    '{"providers":{"p":{"url":"https://host//path/*literal*/"}}}',
])
def test_comment_quotes_never_change_string_state(text):
    assert isinstance(json.loads(o._strip_jsonc(text))['providers'], dict)


def test_root_identity_models_and_env_merge(monkeypatch):
    monkeypatch.setattr(o, '_load_root_providers', lambda: ({'p': {
        'package': 'root', 'name': 'root', 'env': ['ROOT', 'BOTH'],
        'models': {'same': {'root': True}}}}, None))
    caller = {'package': 'caller', 'name': 'caller', 'env': ['BOTH', 'CALLER'],
              'models': {'same': {'caller': True}, 'combo': {'combo': True}}}
    env = o.overlay_env({'OPENCODE_CONFIG_CONTENT': json.dumps({'providers': {'p': caller}})})
    got = json.loads(env['OPENCODE_CONFIG_CONTENT'])['providers']['p']
    assert got == {'package': 'root', 'name': 'root', 'env': ['ROOT', 'BOTH', 'CALLER'],
                   'models': {'same': {'root': True}, 'combo': {'combo': True}}}
    assert r.host_skip_reason({'harness': 'opencode', 'model': 'p/combo'}, env=env, probe=lambda e: set()) is None


@pytest.mark.parametrize('requested,listed', [('omniroute/cx#high', 'omniroute/cx'),
    ('OMNIROUTE/CX', 'omniroute/cx#high')])
def test_provider_model_ids_case_insensitive_and_variants_symmetric(requested, listed):
    assert r.host_skip_reason({'harness': 'opencode', 'model': requested}, probe=lambda e: {listed}) is None


def test_bare_agent_does_not_exempt_other_provider(monkeypatch):
    monkeypatch.setattr(o, '_load_root_providers', lambda: (None, None))
    env = {'OPENCODE_CONFIG_CONTENT': '{"agents":{"cx-x":{}}}'}
    assert r.host_skip_reason({'harness': 'opencode', 'model': 'other/cx-x'}, env=env, probe=lambda e: set()) == 'not_served_on_host: other/cx-x'
    assert r.host_skip_reason({'harness': 'opencode', 'model': 'cx-x'}, env=env, probe=lambda e: set()) is None


def test_unparseable_overlay_is_unknown_one_path_only_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    monkeypatch.setattr(o, '_parse_warned', False)
    path = tmp_path / 'opencode.jsonc'
    path.write_text("{'private-secret': 1}")
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert r.host_skip_reason({'harness': 'opencode', 'model': 'root/model'}, probe=lambda e: set()) is None
    assert len(caplog.records) == 1
    assert str(path) in caplog.text and 'private-secret' not in caplog.text


def test_catalog_failure_episodes_rearm_and_recovery_evicts(caplog):
    class DB:
        broken = True
        calls = 0
        def list_models(self, **kw):
            self.calls += 1
            if self.broken:
                raise OSError('secret')
            return [{'id': 'registered/model', 'active': True, 'available': 1}]
    db, now = DB(), [0]
    r._catalog_failures.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            r.get_catalog_models(db, clock=lambda: now[0])
        assert db.calls == 1 and len(caplog.records) == 1
        now[0] = 31
        r.get_catalog_models(db, clock=lambda: now[0])
        assert db.calls == 2 and len(caplog.records) == 2
        assert 'secret' not in repr(r._catalog_failures)
        assert all(db is not v for entry in r._catalog_failures.values() for v in entry.values())
        db.broken = False
        now[0] = 62
        assert r.get_catalog_models(db, clock=lambda: now[0])[0]['id'] == 'registered/model'
        assert not r._catalog_failures


def test_followup_raises_serves_only_registered_offline_legs(caplog):
    class DB:
        def list_models(self, active_only):
            if active_only:
                return []
            raise OSError('offline')
    r._catalog_failures.clear()
    rows = r.get_catalog_models(DB(), active_only=True)
    assert rows and {m['id'] for m in rows} <= set(r._fallback_legs)
    assert all(m['catalog_source'] == 'offline_fallback_db_error' for m in rows)
    assert 'catalog_db_error' in caplog.text


@pytest.mark.parametrize('all_rows', [[], [{'id': 'inactive/model', 'active': False, 'available': 1}]])
def test_readable_zero_rows_fallback_but_all_inactive_stays_empty(all_rows):
    class DB:
        def list_models(self, active_only):
            return [] if active_only else all_rows
    rows = r.get_catalog_models(DB(), active_only=True)
    if all_rows:
        assert rows == []
    else:
        assert rows and {row['id'] for row in rows} <= set(r._fallback_legs)


def test_absent_overlay_normal_unreadable_overlay_unknown(tmp_path, monkeypatch, caplog):
    assert o.load_root_providers(tmp_path) is None
    (tmp_path / 'opencode.jsonc').mkdir()
    monkeypatch.setattr(o, '_parse_warned', False)
    assert o.load_root_providers(tmp_path) is o.UNKNOWN_PROVIDERS
    assert str(tmp_path / 'opencode.jsonc') in caplog.text


def test_legacy_unknown_fallback_but_explicit_unknown_rejected():
    assert build_standalone_fallback_command('omniroute', 'p', 'x/y') == ['opencode', 'run', '--standalone', '--model', 'x/y']
    assert build_command('p', 'omniroute/x', gateway_online=False)[0] == 'opencode'
    with pytest.raises(ValueError):
        build_command('p', 'x/y', client='omniroute')


def test_probe_presence_key_no_secret_short_empty_and_bounded(monkeypatch):
    calls, now = [], [0]
    monkeypatch.setattr(r, '_probe_served_models', lambda config: calls.append(config) or frozenset())
    r._served_models_real.cache_clear()
    config = '{"providers":{"p":{"env":["W32B_TOKEN"]}}}'
    monkeypatch.delenv('W32B_TOKEN', raising=False)
    r._served_models_real(config, clock=lambda: now[0])
    monkeypatch.setenv('W32B_TOKEN', 'super-private-value')
    r._served_models_real(config, clock=lambda: now[0])
    assert len(calls) == 2
    assert 'super-private-value' not in repr(r._served_cache)
    now[0] = 31
    r._served_models_real(config, clock=lambda: now[0])
    assert len(calls) == 3
    for i in range(15):
        r._served_models_real(json.dumps({'providers': {str(i): {}}}))
    assert len(r._served_cache) <= 8


def test_concurrent_same_key_one_probe_different_keys_not_serialized(monkeypatch):
    calls, barrier = [], threading.Barrier(2)
    def probe(config):
        calls.append(config)
        barrier.wait(timeout=2)
        time.sleep(.05)
        return frozenset({'x/y'})
    monkeypatch.setattr(r, '_probe_served_models', probe)
    r._served_models_real.cache_clear()
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(r._served_models_real, cfg) for cfg in ('{}', '{}', '{"providers":{"other":{}}}')]
        assert all(f.result() == {'x/y'} for f in futures)
    assert len(calls) == 2


def test_concurrent_callers_share_one_subprocess(monkeypatch):
    import subprocess
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        time.sleep(.05)
        return SimpleNamespace(returncode=0, stdout='provider/model\n')
    monkeypatch.setattr(subprocess, 'run', run)
    r._served_models_real.cache_clear()
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(r._served_models_real, '{}') for _ in range(2)]
        assert all(f.result() == {'provider/model'} for f in futures)
    assert len(calls) == 1


def test_async_launch_plan_does_not_block_tick(monkeypatch):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    leg = next(leg for leg in DEFAULT_LEGS if leg.harness == 'opencode')
    started, release = threading.Event(), threading.Event()
    def probe(env):
        started.set()
        assert release.wait(2)
        return None
    async def run():
        task = asyncio.create_task(r.plan_for_launch_async(SpawnPlan((leg,), (), 'test', (), ()), probe=probe))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(.005)
        assert started.is_set() and not task.done()
        release.set()
        assert (await task).legs == (leg,)
    asyncio.run(run())
