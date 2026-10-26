import sys

import pytest

from plangraph.routing_support import opencode_overlay as o, resolver as r


@pytest.fixture(autouse=True)
def root_config(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    monkeypatch.setattr(o, '_missing_warned', False, raising=False)
    monkeypatch.setattr(o, '_missing_path', None, raising=False)
    monkeypatch.setattr(r, 'admitted_by_render', lambda model: False)
    monkeypatch.setattr(r, 'host_probe_env', lambda env: o.overlay_env(env or {}))
    return tmp_path / 'opencode.jsonc'


def test_missing_root_warns_once(root_config, caplog):
    for _ in range(2):
        assert o.load_root_providers() is None
    warnings = [rec.message for rec in caplog.records
                if 'root_provider_config_missing' in rec.message]
    assert warnings == [f'root_provider_config_missing path={root_config}']


def test_missing_root_not_served_reason(root_config):
    reason = r.host_skip_reason({'harness': 'opencode', 'model': 'test/absent'},
                                probe=lambda env: set())
    assert reason == (f'not_served_on_host: test/absent '
                      f'(root_provider_config_missing: {root_config})')


def test_present_root_reason_unchanged(root_config, caplog):
    o.load_root_providers()  # A recovered config must not retain stale context.
    caplog.clear()
    root_config.write_text('{"providers": {}}', encoding='utf-8')
    assert o.load_root_providers() == {}
    assert r.host_skip_reason({'harness': 'opencode', 'model': 'test/absent'},
                              probe=lambda env: set()) == 'not_served_on_host: test/absent'
    assert not caplog.records


def test_utf8_root_parses(root_config, monkeypatch):
    root_config.write_text('{"providers": {"test": {"name": "caf\u00e9"}}}', encoding='utf-8')
    original = type(root_config).read_text
    def read_text(path, *args, **kwargs):
        assert kwargs.get('encoding') == 'utf-8'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(root_config), 'read_text', read_text)
    assert o.load_root_providers() == {'test': {'name': 'caf\u00e9'}}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
