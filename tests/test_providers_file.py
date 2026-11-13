"""Owned provider defaults, operator overrides, and registered-leg drift."""
import json
from pathlib import Path
import re

import pytest

from plangraph.planner.legs import spawnable_legs
from plangraph.routing_support import opencode_overlay as o, resolver as r

SHIPPED_FILE = Path(__file__).resolve().parents[1] / 'configuration/opencode/providers.json'
# Raw model keys, including explicit effort legs rendered per run by opencode_render.
OWNER_ITEM = 'plangraph:PG-OMNIROUTE-CONFIG-HOME'
EXPECTED_MISSING = {
    'cx-gpt-6-luna-high': OWNER_ITEM,
    'cx-gpt-6-luna-low': OWNER_ITEM,
    'cx-gpt-6-luna-max': OWNER_ITEM,
    'cx-gpt-6-luna-medium': OWNER_ITEM,
    'cx-gpt-6-luna-xhigh': OWNER_ITEM,
    'cx-gpt-6.1-sol-high': OWNER_ITEM,
    'cx-gpt-6.1-sol-low': OWNER_ITEM,
    'cx-gpt-6.1-sol-max': OWNER_ITEM,
    'cx-gpt-6.1-sol-medium': OWNER_ITEM,
    'cx-gpt-6.1-sol-xhigh': OWNER_ITEM,
    'deepseek-flash': OWNER_ITEM,
    'cx-gpt-6.1-terra-medium': OWNER_ITEM,
    'cx-gpt-6.1-terra-xhigh': OWNER_ITEM,
    'gc/grok-4.7': OWNER_ITEM,
    'muse-ember-max': OWNER_ITEM,
    'muse-spark-1.3-contributor': OWNER_ITEM,
}
EXPECTED_EXTRA = {
    'agy-claude-sonnet-5-5-high': OWNER_ITEM, 'agy-gemini-3.8-flash': OWNER_ITEM,
    'auto': OWNER_ITEM, 'auto/cheap': OWNER_ITEM, 'auto/smart': OWNER_ITEM,
    'l1-orchestrator': OWNER_ITEM, 'l1-orchestrator-clean': OWNER_ITEM,
    'l1-orchestrator-free-only': OWNER_ITEM, 'l2-orchestrator': OWNER_ITEM,
    'l2-orchestrator-clean': OWNER_ITEM, 'l2-researcher': OWNER_ITEM,
    'l2-worker': OWNER_ITEM, 'l2-worker-1m': OWNER_ITEM, 'l2-worker-clean': OWNER_ITEM,
    'l2-worker-free-only': OWNER_ITEM, 'l3-driver': OWNER_ITEM,
    'l3-driver-1m': OWNER_ITEM, 'l3-driver-256k': OWNER_ITEM, 'l3-driver-clean': OWNER_ITEM,
    'l3-driver-free-only': OWNER_ITEM, 'l3-implementer': OWNER_ITEM,
    'l3-implementer-clean': OWNER_ITEM, 'l3-researcher': OWNER_ITEM,
    'l3-researcher-clean': OWNER_ITEM, 'l3-review-codebase': OWNER_ITEM,
    'l3-review-diff': OWNER_ITEM, 'l3-review-tests': OWNER_ITEM,
    'meta-direct-muse-spark-1.3-contributor': OWNER_ITEM,
    'opencode-go/glm-5.3-flash': OWNER_ITEM, 'opencode-go/kimi-k3': OWNER_ITEM,
    'opencode-go/minimax-m3': OWNER_ITEM, 'opencode-go/qwen3.8-flash': OWNER_ITEM,
    'or-laguna-s-2.1-free': OWNER_ITEM, 'or-nemotron-3-super-free': OWNER_ITEM,
    'or-north-mini-code-free': OWNER_ITEM, 't4-rag': OWNER_ITEM,
    'vertex-3.6-flash': OWNER_ITEM, 'vertex-3.7-flash': OWNER_ITEM,
    'vertex-3.8-flash': OWNER_ITEM, 'vertex-gemini-3.8-flash': OWNER_ITEM,
}


def env_entries(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'env':
                yield value
            else:
                yield from env_entries(value)
    elif isinstance(node, list):
        for value in node:
            yield from env_entries(value)


@pytest.mark.parametrize('override', [None, ''])
def test_shipped_providers_are_safe(monkeypatch, override):
    monkeypatch.delenv('PLANGRAPH_PROVIDERS_FILE')
    if override is not None:
        monkeypatch.setenv('PLANGRAPH_PROVIDERS_FILE', override)
    providers = o.load_root_providers()
    assert set(providers) == {'opencode', 'mimo', 'omniroute'}
    assert providers == json.loads(SHIPPED_FILE.read_text(encoding='utf-8'))['providers']
    for entries in env_entries(providers):
        assert isinstance(entries, list)
        assert all(isinstance(name, str) and re.fullmatch(r'[A-Z][A-Z0-9_]*', name)
                   for name in entries)
    assert not re.search(r'sk-[A-Za-z0-9]{8,}', SHIPPED_FILE.read_text(encoding='utf-8'))


def test_providers_file_override_wins(tmp_path, monkeypatch):
    path = tmp_path / 'operator.json'
    path.write_text('{"providers": {"operator": {}}}', encoding='utf-8')
    monkeypatch.setenv('PLANGRAPH_PROVIDERS_FILE', str(path))
    assert o.load_root_providers() == {'operator': {}}
    assert o.load_root_providers(path=SHIPPED_FILE) == json.loads(
        SHIPPED_FILE.read_text(encoding='utf-8'))['providers']


def test_autoos_root_does_not_change_providers(tmp_path, monkeypatch):
    (tmp_path / 'opencode.jsonc').write_text(
        '{"providers": {"external-only": {}}}', encoding='utf-8')
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_PROVIDERS_FILE')
    assert o.load_root_providers() == json.loads(
        SHIPPED_FILE.read_text(encoding='utf-8'))['providers']


@pytest.mark.parametrize('broken', ['missing', 'parse_error', 'unreadable'])
def test_broken_providers_file_is_loud(tmp_path, monkeypatch, caplog, broken):
    path = tmp_path / 'providers.json'
    if broken == 'parse_error':
        path.write_text('{private-invalid', encoding='utf-8')
    elif broken == 'unreadable':
        path.mkdir()
    monkeypatch.setenv('PLANGRAPH_PROVIDERS_FILE', str(path))
    monkeypatch.setattr(o, '_missing_warned', False)
    monkeypatch.setattr(o, '_parse_warned', False)
    monkeypatch.setattr(r, 'admitted_by_render', lambda model: False)
    monkeypatch.setattr(r, 'host_probe_env', lambda env: o.overlay_env(env or {}))
    missing = broken == 'missing'
    expected = (f'not_served_on_host: test/absent '
                f'(root_provider_config_missing: {path})') if missing else f'root_provider_config_broken: {path}'
    for _ in range(2):
        assert o._load_root_providers(path) == ((None, path) if missing else (o.UNKNOWN_PROVIDERS, path))
        assert r.host_skip_reason({'harness': 'opencode', 'model': 'test/absent'},
                                  probe=lambda env: set()) == expected
    key = broken
    assert [rec.message for rec in caplog.records] == [f'root_provider_config_{key} path={path}']


def test_broken_marker_preserves_unknown_without_leaking(tmp_path, monkeypatch):
    path = tmp_path / 'providers.json'
    path.write_text('{invalid', encoding='utf-8')
    monkeypatch.setenv('PLANGRAPH_PROVIDERS_FILE', str(path))
    broken = o.overlay_env({})
    assert broken[o.OVERLAY_ROOT_BROKEN] == str(path)
    assert broken[o.OVERLAY_UNKNOWN] == '1'
    monkeypatch.setattr(r, 'host_probe_env', lambda env: o.overlay_env(env or {}))
    caller = {**broken, 'OPENCODE_CONFIG_CONTENT': '{invalid'}
    assert o.OVERLAY_ROOT_BROKEN not in o.overlay_env(caller)
    assert r.host_skip_reason({'harness': 'opencode', 'model': 'test/absent'}, env=caller) is None
    path.write_text('{"providers": {}}', encoding='utf-8')
    assert o.OVERLAY_ROOT_BROKEN not in o.overlay_env(broken)


def test_omniroute_leg_models_do_not_drift():
    # service/target and routing.validate_model share planner.legs' registered catalog.
    models = {leg.model.removeprefix('omniroute/') for leg in spawnable_legs()
              if leg.model.startswith('omniroute/')}
    providers = o.load_root_providers(SHIPPED_FILE)
    missing = models - providers['omniroute']['models'].keys()
    assert missing == EXPECTED_MISSING.keys(), (
        f'missing model drift: {sorted(missing ^ EXPECTED_MISSING.keys())}; owner: {OWNER_ITEM}')
    extra = providers['omniroute']['models'].keys() - models
    assert extra == EXPECTED_EXTRA.keys(), (
        f'extra model drift: {sorted(extra ^ EXPECTED_EXTRA.keys())}; owner: {OWNER_ITEM}')
