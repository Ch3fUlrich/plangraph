"""Host key handoff to child runs (plangraph:MODEL-ROUTE-SPAWN, D-2759).

Hermetic: every case points AUTOOS_ROOT at a tmp_path fake root whose
``tools/`` helpers are stubs returning fake strings. Nothing here reads
``configuration/api-keys.yml`` or any real key.
"""
import pytest

from plangraph.routing_support.host_keys import (
    GATEWAY_KEY_ENV,
    ZEN_KEY_ENV,
    add_host_keys,
    redaction_env,
)
from plangraph.spawner.env_redact import redact_env_values

GATEWAY_FAKE = 'fake-gateway-key-AAA111'
ZEN_FAKE = 'fake-zen-key-BBB222'
PARENT_GATEWAY = 'parent-env-gateway-key-333'
PARENT_ZEN = 'parent-env-zen-key-444'

GATEWAY_STUB = 'def resolve_client_key(env=None, keys_file=None):\n    return %r\n' % GATEWAY_FAKE
ZEN_STUB = 'def read_keys(path):\n    return {\"zen\": %r}\n' % ZEN_FAKE
RAISING_STUB = 'raise RuntimeError(\'stub helper import failure\')\n'


def _fake_root(tmp_path):
    root = tmp_path / 'fake-autoos'
    (root / 'tools').mkdir(parents=True)
    (root / 'configuration').mkdir()
    # Fake keys file; the stub helpers never read it and no real key exists here.
    (root / 'configuration' / 'api-keys.yml').write_text('omniroute_server: fake-not-a-key\nzen: fake-not-a-key\n')
    (root / 'tools' / 'autoos_gateway_key.py').write_text(GATEWAY_STUB)
    (root / 'tools' / 'keys_file.py').write_text(ZEN_STUB)
    return root


@pytest.fixture
def fake_autoos(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOOS_ROOT', str(_fake_root(tmp_path)))
    return tmp_path / 'fake-autoos'


def test_gateway_key_for_mimo_needs_gateway_and_no_zen(fake_autoos):
    env = {}
    add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True)
    assert env == {GATEWAY_KEY_ENV: GATEWAY_FAKE}
    assert ZEN_KEY_ENV not in env


def test_existing_key_kept_and_zen_key_for_free_model(fake_autoos):
    env = {GATEWAY_KEY_ENV: PARENT_GATEWAY}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert env[GATEWAY_KEY_ENV] == PARENT_GATEWAY  # an existing env value always wins
    assert env[ZEN_KEY_ENV] == ZEN_FAKE

    # "#variant" is stripped before the -free check; an existing Zen key is kept.
    variant = {ZEN_KEY_ENV: PARENT_ZEN}
    add_host_keys(variant, 'opencode/glm-5.3-flash-free#high', needs_gateway=True)
    assert variant[ZEN_KEY_ENV] == PARENT_ZEN
    assert variant[GATEWAY_KEY_ENV] == GATEWAY_FAKE


def test_no_gateway_key_when_not_needed(fake_autoos):
    env = {}
    add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=False)
    assert env == {}


def test_missing_root_leaves_env_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path / 'absent-root'))
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert env == {}
    assert redaction_env({'PATH': '/bin'}) == {'PATH': '/bin'}


def test_helper_raising_at_import_gives_no_key(fake_autoos):
    root = fake_autoos
    (root / 'tools' / 'autoos_gateway_key.py').write_text(RAISING_STUB)
    (root / 'tools' / 'keys_file.py').write_text(RAISING_STUB)
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert env == {}
    assert redaction_env({}) == {}


def test_redaction_env_is_a_copy_carrying_both_keys(fake_autoos):
    base = {'PATH': '/bin'}
    env = redaction_env(base)
    assert base == {'PATH': '/bin'}  # never mutates the base
    assert env is not base
    assert env[GATEWAY_KEY_ENV] == GATEWAY_FAKE
    assert env[ZEN_KEY_ENV] == ZEN_FAKE

    # Where absent only: a resolvable key already in base is not displaced.
    assert redaction_env({GATEWAY_KEY_ENV: PARENT_GATEWAY})[GATEWAY_KEY_ENV] == PARENT_GATEWAY

    text = 'out gateway=%s zen=%s' % (GATEWAY_FAKE, ZEN_FAKE)
    assert redact_env_values(text, env) == 'out gateway=[autoos:redacted] zen=[autoos:redacted]'
