import asyncio
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from plangraph.spawner import resolver as r
from plangraph.spawner import opencode_overlay as o
from plangraph.spawner.clients import build_child_env, build_command


def test_root_definition_beats_generated_defaults(monkeypatch):
    monkeypatch.setattr(o, 'load_root_providers', lambda: {'omniroute': {
        'env': ['OMNI_REAL_KEY'], 'package': 'real-pkg', 'name': 'real', 'models': {'alias': {}}}})
    cfg = json.loads(o.overlay_env(build_child_env(headers={'X-OmniRoute-Run-Nonce': 'n1'}))['OPENCODE_CONFIG_CONTENT'])
    assert cfg['providers']['omniroute']['env'] == ['OMNI_REAL_KEY']
    assert cfg['providers']['omniroute']['package'] == 'real-pkg'


@pytest.mark.parametrize('model', [None, 'l2-worker', 'omniroute/alias'])
def test_unknown_and_declared_alias_admitted(model):
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps({'providers': {'omniroute': {'models': {'alias': {'combo': True}}}}})}
    assert r.host_skip_reason({'model': model, 'harness': 'opencode'}, env=env, probe=lambda e: {'x/y'}) is None


def test_listing_case_variant_and_absence():
    probe = lambda e: {'omniroute/cx-gpt-6.1-sol#high'}
    assert r.host_skip_reason({'model': 'OMNIROUTE/CX-GPT-6.1-SOL', 'harness': 'opencode'}, probe=probe) is None
    assert r.host_skip_reason({'model': 'other/absent', 'harness': 'opencode'}, probe=probe) == 'not_served_on_host: other/absent'


def test_real_probe_stable_nonce_ttl_and_short_unknown(monkeypatch):
    calls = []
    now = [0]
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: calls.append(kw) or SimpleNamespace(returncode=0, stdout='Models\nomniroute/cx-gpt-6.1-sol#high\nmimo/mimo-v2.6-flash\n'))
    probe = r._served_models_real
    probe.cache_clear()
    def config(n):
        return json.dumps({'providers': {'omniroute': {'headers': {'X-OmniRoute-Run-Nonce': n}}}})
    assert 'mimo/mimo-v2.6-flash' in probe(config('n1'), clock=lambda: now[0])
    probe(config('n2'), clock=lambda: now[0])
    assert len(calls) == 1
    now[0] = 301
    probe(config('n2'), clock=lambda: now[0])
    assert len(calls) == 2
    probe.cache_clear()
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: calls.append(kw) or SimpleNamespace(returncode=1))
    probe('{}', clock=lambda: now[0])
    now[0] += 31
    probe('{}', clock=lambda: now[0])
    assert len(calls) == 4


def test_catalog_failure_episode_and_listing(monkeypatch, caplog):
    class Broken:
        calls = 0
        def list_models(self, **kw):
            self.calls += 1
            raise OSError('offline')
    db = Broken()
    now = [0]
    monkeypatch.setattr(r, 'DEFAULT_FALLBACK_MODELS', [{'id': 'x/y', 'active': True, 'available': 0}])
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            assert r.get_catalog_models(db, clock=lambda: now[0])
        assert r.get_catalog_models(db, active_only=True, clock=lambda: now[0]) == []
        assert db.calls == 1
        assert len([x for x in caplog.records if 'catalog_db_error' in x.message]) == 1
        now[0] = 31
        r.get_catalog_models(db, clock=lambda: now[0])
        assert db.calls == 2
        assert len([x for x in caplog.records if 'catalog_db_error' in x.message]) == 1


def test_second_query_failure_fail_closed_without_last_good_rows():
    class Broken:
        def list_models(self, active_only):
            if active_only:
                return []
            raise OSError('unknown inactive catalog')
    assert r.get_catalog_models(Broken(), active_only=True) == []


@pytest.mark.parametrize('client', ['gemini', 'qwen', 'codex'])
def test_native_harness_preserves_model(client):
    argv = build_command('prompt', 'provider/model', client=client)
    assert argv == ([client, 'exec', '--model', 'provider/model', 'prompt'] if client == 'codex' else [client, '-p', 'prompt', '--model', 'provider/model'])


def test_native_runner_does_not_apply_overlay(monkeypatch):
    from plangraph.spawner.runner import SpawnerRunner
    def forbidden(*a, **kw):
        raise AssertionError('native argv must not receive provider overlay')
    monkeypatch.setattr(o, 'overlay_env', forbidden)
    for client in ('gemini', 'qwen', 'codex'):
        result = asyncio.run(SpawnerRunner().spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash',
                                                  client=client, dry_run=True))
        assert result


def test_async_probe_offloads_real_function(monkeypatch):
    import threading
    main_thread = threading.get_ident()
    seen = []
    monkeypatch.setattr(r, '_served_models_real', lambda config, **kw: seen.append(threading.get_ident()) or frozenset())
    assert asyncio.run(r.served_models_async('{}')) == frozenset()
    assert seen[0] != main_thread


def test_jsonc_parse_warning_once_without_values(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(o, '_parse_warned', False)
    (tmp_path / 'opencode.jsonc').write_text('{private-value-invalid')
    with caplog.at_level(logging.WARNING):
        assert o.load_root_providers(tmp_path) is None
        assert o.load_root_providers(tmp_path) is None
    assert len(caplog.records) == 1
    assert 'private-value' not in caplog.text


@pytest.mark.parametrize('text', [
    '{"providers": {}, // trailing\n"x": 1}',
    '{/* block */ "providers": {}}',
    '{"providers": {"x": {"url": "https://host/*keep*/"}},}',
])
def test_jsonc_comments_strings_trailing_comma(tmp_path, text):
    (tmp_path / 'opencode.jsonc').write_text(text)
    providers = o.load_root_providers(tmp_path)
    assert providers is not None
    if providers:
        assert providers['x']['url'] == 'https://host/*keep*/'


def test_internal_doc_assessment_not_caller_assertion():
    assert r.resolve_review_plan('test/writer', paths=['README.md'], is_pure_doc=True,
                                 bucket='S0', reviewer_count=0)['review_exempt']


def test_plan_and_launch_share_provider_digest(monkeypatch):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    seen = []
    probe = lambda env: seen.append(r.provider_config_digest(env['OPENCODE_CONFIG_CONTENT'])) or None
    leg = next(leg for leg in DEFAULT_LEGS if leg.harness == 'opencode')
    r.plan_for_launch(SpawnPlan((leg,), (), 'test', (), ()), probe=probe)
    r.host_skip_reason({'harness': 'opencode', 'model': 'x/y'}, env=build_child_env(), probe=probe)
    assert len(seen) == 2 and seen[0] == seen[1]


def test_plan_skipped_canonical_reason_with_w30_compatibility_alias():
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    leg = next(leg for leg in DEFAULT_LEGS if leg.harness == 'opencode' and '/' in leg.model)
    plan = r.plan_for_launch(SpawnPlan((leg,), (), 'test', (), ()), probe=lambda env: set())
    assert set(plan.skipped[0]) == {'leg', 'reason', 'skip_reason'}
    assert plan.skipped[0]['reason'] == plan.skipped[0]['skip_reason']
