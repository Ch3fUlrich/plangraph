"""Host key handoff to child runs (plangraph:MODEL-ROUTE-SPAWN, D-2759).

Hermetic: every case points AUTOOS_ROOT at a tmp_path fake root whose
``tools/`` helpers are stubs returning fake strings. Nothing here reads
``configuration/api-keys.yml`` or any real key.
"""
import logging
import os

import pytest

from plangraph.routing_support import host_keys
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
    monkeypatch.setenv('HOME', str(tmp_path / 'fake-home'))
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
    monkeypatch.setenv('HOME', str(tmp_path / 'fake-home'))
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


# --- round 1 (plangraph:MODEL-ROUTE-SPAWN): helper failures never crash ---
# A YAML-like custom Exception (the shape yaml.YAMLError has), an AttributeError,
# a sys.exit() at import or at call time, a non-dict read_keys result, and a
# non-str or blank key must each mean "no key" -- never an escape into the
# supervisor and never a key value in an exception, log or print.

GATEWAY_YAML_CALL_STUB = '''class YamlLikeError(Exception):
    pass

def resolve_client_key(env=None, keys_file=None):
    raise YamlLikeError('stub yaml failure')
'''
ZEN_YAML_CALL_STUB = '''class YamlLikeError(Exception):
    pass

def read_keys(path):
    raise YamlLikeError('stub yaml failure')
'''
GATEWAY_ATTR_CALL_STUB = '''def resolve_client_key(env=None, keys_file=None):
    raise AttributeError('stub attribute failure')
'''
ZEN_ATTR_CALL_STUB = '''def read_keys(path):
    raise AttributeError('stub attribute failure')
'''
EXIT_IMPORT_STUB = '''import sys
sys.exit(0)
'''
GATEWAY_EXIT_CALL_STUB = '''import sys

def resolve_client_key(env=None, keys_file=None):
    sys.exit(0)
'''
ZEN_EXIT_CALL_STUB = '''import sys

def read_keys(path):
    sys.exit(0)
'''
ZEN_LIST_STUB = '''def read_keys(path):
    return ['zen', 'not-a-dict']
'''
ZEN_NONSTR_STUB = '''def read_keys(path):
    return {'zen': 12345}
'''
ZEN_BLANK_STUB = '''def read_keys(path):
    return {'zen': '   '}
'''
GATEWAY_NONSTR_STUB = '''def resolve_client_key(env=None, keys_file=None):
    return 12345
'''
GATEWAY_BLANK_STUB = '''def resolve_client_key(env=None, keys_file=None):
    return '   '
'''
# Helpers whose raised message embeds the fake key: nothing may surface it.
GATEWAY_LEAK_STUB = '''def resolve_client_key(env=None, keys_file=None):
    raise RuntimeError(%r)
''' % ('boom with ' + GATEWAY_FAKE)
ZEN_LEAK_STUB = '''def read_keys(path):
    raise RuntimeError(%r)
''' % ('boom with ' + ZEN_FAKE)
KEYBOARD_INTERRUPT_IMPORT_STUB = 'raise KeyboardInterrupt()'


def _write_helper(root, name, code):
    (root / 'tools' / (name + '.py')).write_text(code)


class _BrokenEnv(dict):
    """dict whose .get raises -- the entry points must survive it."""

    def get(self, *args, **kwargs):
        raise RuntimeError('broken get')


class _PartiallyBrokenBase:
    """Mapping whose dict() copy raises; the readable items must survive."""

    def keys(self):
        return ['PATH', 'BROKEN', 'HOME']

    def __getitem__(self, key):
        if key == 'BROKEN':
            raise RuntimeError('broken getitem')
        return {'PATH': '/bin', 'HOME': '/synthetic-home/x'}[key]


class _ExplodingBase(_PartiallyBrokenBase):
    """Copy raises before any item: nothing enumerable, even item by item."""

    def keys(self):
        raise RuntimeError('broken keys')


def _assert_only_other_helper_resolves(helper_name):
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    red = redaction_env({'PATH': '/bin'})
    if helper_name == 'autoos_gateway_key':
        assert env == {ZEN_KEY_ENV: ZEN_FAKE}
        assert red == {'PATH': '/bin', ZEN_KEY_ENV: ZEN_FAKE}
    else:
        assert env == {GATEWAY_KEY_ENV: GATEWAY_FAKE}
        assert red == {'PATH': '/bin', GATEWAY_KEY_ENV: GATEWAY_FAKE}


@pytest.mark.parametrize('helper_name,stub', [
    pytest.param('autoos_gateway_key', GATEWAY_YAML_CALL_STUB, id='gateway-yaml-like'),
    pytest.param('keys_file', ZEN_YAML_CALL_STUB, id='zen-yaml-like'),
    pytest.param('autoos_gateway_key', GATEWAY_ATTR_CALL_STUB, id='gateway-attribute'),
    pytest.param('keys_file', ZEN_ATTR_CALL_STUB, id='zen-attribute'),
])
def test_call_time_helper_raise_means_no_key(fake_autoos, helper_name, stub):
    # H1: ANY Exception from the helper call (resolve_client_key, read_keys)
    # means None; the other helper is unharmed and nothing raises.
    _write_helper(fake_autoos, helper_name, stub)
    _assert_only_other_helper_resolves(helper_name)


@pytest.mark.parametrize('helper_name,stub', [
    pytest.param('autoos_gateway_key', GATEWAY_EXIT_CALL_STUB, id='gateway'),
    pytest.param('keys_file', ZEN_EXIT_CALL_STUB, id='zen'),
])
def test_call_time_sys_exit_means_no_key(fake_autoos, helper_name, stub):
    # H5: a helper calling sys.exit() at call time must not crash the
    # supervisor either -- same pattern as the import-time SystemExit gap.
    _write_helper(fake_autoos, helper_name, stub)
    _assert_only_other_helper_resolves(helper_name)


@pytest.mark.parametrize('helper_name', ['autoos_gateway_key', 'keys_file'])
def test_sys_exit_at_import_means_no_key(fake_autoos, helper_name):
    # H2: Exception or SystemExit at import -> None.
    _write_helper(fake_autoos, helper_name, EXIT_IMPORT_STUB)
    _assert_only_other_helper_resolves(helper_name)


def test_keyboard_interrupt_at_import_propagates(fake_autoos):
    # H2 names the exception NOT to swallow: a Ctrl-C must still interrupt.
    _write_helper(fake_autoos, 'autoos_gateway_key', KEYBOARD_INTERRUPT_IMPORT_STUB)
    with pytest.raises(KeyboardInterrupt):
        add_host_keys({}, 'mimo/mimo-v2.6-pro', needs_gateway=True)


def test_read_keys_returning_non_dict_means_no_key(fake_autoos):
    # H1: .get on a non-dict must not escape.
    _write_helper(fake_autoos, 'keys_file', ZEN_LIST_STUB)
    _assert_only_other_helper_resolves('keys_file')


@pytest.mark.parametrize('gateway_stub,zen_stub', [
    pytest.param(GATEWAY_NONSTR_STUB, ZEN_NONSTR_STUB, id='non-str'),
    pytest.param(GATEWAY_BLANK_STUB, ZEN_BLANK_STUB, id='blank'),
])
def test_non_str_or_blank_key_means_no_key(fake_autoos, gateway_stub, zen_stub):
    # H3: a non-str or blank key -> None, for both lookups.
    _write_helper(fake_autoos, 'autoos_gateway_key', gateway_stub)
    _write_helper(fake_autoos, 'keys_file', zen_stub)
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert env == {}
    assert redaction_env({'PATH': '/bin'}) == {'PATH': '/bin'}


@pytest.mark.parametrize('helper_name', ['autoos_gateway_key', 'keys_file'])
def test_absent_helper_file_means_no_key(fake_autoos, helper_name):
    # H4: a missing helper under an existing root -> no key, nothing raises.
    (fake_autoos / 'tools' / (helper_name + '.py')).unlink()
    _assert_only_other_helper_resolves(helper_name)


def test_absent_helper_files_leave_env_unchanged(fake_autoos):
    # H4: both helpers absent -> no key at all, env unchanged.
    (fake_autoos / 'tools' / 'autoos_gateway_key.py').unlink()
    (fake_autoos / 'tools' / 'keys_file.py').unlink()
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert env == {}
    assert redaction_env({'PATH': '/bin'}) == {'PATH': '/bin'}


def test_absent_keys_file_means_no_key(fake_autoos):
    # H4: the keys file absent with the helpers present -> no key, env unchanged.
    (fake_autoos / 'configuration' / 'api-keys.yml').unlink()
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert env == {}
    assert redaction_env({'PATH': '/bin'}) == {'PATH': '/bin'}


def test_failures_never_raise_or_leak_key_values(fake_autoos, capsys):
    # H5: the helpers raise with the fake key in the message; nothing may
    # surface that message in an exception, a log or a print.
    _write_helper(fake_autoos, 'autoos_gateway_key', GATEWAY_LEAK_STUB)
    _write_helper(fake_autoos, 'keys_file', ZEN_LEAK_STUB)
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert env == {}
    assert redaction_env({'PATH': '/bin'}) == {'PATH': '/bin'}
    captured = capsys.readouterr()
    assert GATEWAY_FAKE not in captured.out + captured.err
    assert ZEN_FAKE not in captured.out + captured.err


def test_add_host_keys_never_raises_on_broken_env(fake_autoos):
    # H5: add_host_keys never raises, whatever env does.
    env = _BrokenEnv()
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=True)
    assert dict(env) == {}


# --- round 2 (router D-2776): redaction_env never drops a name it could carry ---

ZEN_MAPPING_STUB = 'from types import MappingProxyType\ndef read_keys(path):\n    return MappingProxyType({"zen": %r})\n' % ZEN_FAKE


def test_redaction_env_keeps_every_name_when_base_copy_raises(fake_autoos):
    # D-2776: dict(base) raising must NOT return {} -- base items are copied
    # one by one (only the item whose read raises is skipped) and BOTH fake
    # keys are in the result.
    env = redaction_env(_PartiallyBrokenBase())
    assert env == {'PATH': '/bin', 'HOME': '/synthetic-home/x',
                   GATEWAY_KEY_ENV: GATEWAY_FAKE, ZEN_KEY_ENV: ZEN_FAKE}
    # Nothing enumerable at all: the resolvable keys are still carried.
    assert redaction_env(_ExplodingBase()) == {
        GATEWAY_KEY_ENV: GATEWAY_FAKE, ZEN_KEY_ENV: ZEN_FAKE}


def test_one_failing_resolver_never_skips_the_other(fake_autoos, monkeypatch):
    # D-2776: each key resolution runs in its own try (_BrokenEnv.get raises
    # on any call: stand-in for a failing resolver).
    monkeypatch.setattr(host_keys, '_gateway_key', _BrokenEnv().get)
    assert redaction_env({'PATH': '/bin'}) == {'PATH': '/bin', ZEN_KEY_ENV: ZEN_FAKE}


def test_read_keys_returning_any_mapping_resolves_zen(fake_autoos):
    # D-2776: read_keys may return any Mapping, not only a dict (a
    # non-Mapping stays "no key": round 1's list case).
    _write_helper(fake_autoos, 'keys_file', ZEN_MAPPING_STUB)
    env = {}
    add_host_keys(env, 'opencode/glm-5.3-flash-free', needs_gateway=False)
    assert env == {ZEN_KEY_ENV: ZEN_FAKE}
    assert redaction_env({})[ZEN_KEY_ENV] == ZEN_FAKE


# --- plangraph:GATEWAY-CLIENT-KEY-PLANGRAPH-FILE-S1: per-host client key file ---

CLIENT_KEY_SENTINEL = 'sk-plangraph-client-sentinel-777'


def _write_client_key(home, content, *, file_mode=0o600, dir_mode=0o700):
    key_dir = home / '.config' / 'plangraph'
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / 'omniroute-client.key'
    if isinstance(content, bytes):
        key_path.write_bytes(content)
    else:
        key_path.write_text(content, encoding='utf-8')
    key_path.chmod(file_mode)
    key_dir.chmod(dir_mode)
    return key_dir, key_path


def test_client_key_file_0600_is_returned_and_wins_over_autoos(tmp_path, monkeypatch, fake_autoos, caplog):
    home = tmp_path / 'home-0600'
    monkeypatch.setenv('HOME', str(home))
    _write_client_key(home, f'  {CLIENT_KEY_SENTINEL} \n')

    with caplog.at_level(logging.WARNING):
        assert host_keys._gateway_key({}) == CLIENT_KEY_SENTINEL
        env = {}
        add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True)
        assert env == {GATEWAY_KEY_ENV: CLIENT_KEY_SENTINEL}
        assert redaction_env({'PATH': '/bin'})[GATEWAY_KEY_ENV] == CLIENT_KEY_SENTINEL

    assert not caplog.records
    assert CLIENT_KEY_SENTINEL not in caplog.text


def test_client_key_stricter_0400_file_0500_dir_is_returned(tmp_path, monkeypatch):
    home = tmp_path / 'home-strict'
    monkeypatch.setenv('HOME', str(home))
    key_dir, key_path = _write_client_key(home, CLIENT_KEY_SENTINEL + '\n', file_mode=0o400, dir_mode=0o500)
    try:
        assert host_keys._gateway_key({}) == CLIENT_KEY_SENTINEL
    finally:
        key_dir.chmod(0o700)
        key_path.chmod(0o600)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
@pytest.mark.parametrize('file_mode', [0o644, 0o640, 0o604, 0o660, 0o601])
def test_client_key_group_or_other_accessible_file_refused_with_warning(
    tmp_path, monkeypatch, fake_autoos, caplog, capsys, file_mode
):
    home = tmp_path / f'home-mode-{file_mode:04o}'
    monkeypatch.setenv('HOME', str(home))
    _, key_path = _write_client_key(home, CLIENT_KEY_SENTINEL + '\n', file_mode=file_mode)

    with caplog.at_level(logging.WARNING):
        assert host_keys._gateway_key({}) is None
        env = {}
        add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True)
        assert env == {}

    assert len(caplog.records) == 2
    for rec in caplog.records:
        assert str(key_path) in rec.message
        assert f'{file_mode:04o}' in rec.message
        assert CLIENT_KEY_SENTINEL not in rec.message
    captured = capsys.readouterr()
    assert CLIENT_KEY_SENTINEL not in captured.out + captured.err


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
def test_client_key_0755_dir_refused_with_warning(tmp_path, monkeypatch, fake_autoos, caplog, capsys):
    home = tmp_path / 'home-dir-0755'
    monkeypatch.setenv('HOME', str(home))
    key_dir, _ = _write_client_key(home, CLIENT_KEY_SENTINEL + '\n', file_mode=0o600, dir_mode=0o755)

    with caplog.at_level(logging.WARNING):
        assert host_keys._gateway_key({}) is None
        env = {}
        add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True)
        assert env == {}

    assert len(caplog.records) == 2
    for rec in caplog.records:
        assert str(key_dir) in rec.message
        assert '0755' in rec.message
        assert CLIENT_KEY_SENTINEL not in rec.message
    captured = capsys.readouterr()
    assert CLIENT_KEY_SENTINEL not in captured.out + captured.err


def test_client_key_symlink_refused_with_warning(tmp_path, monkeypatch, fake_autoos, caplog, capsys):
    home = tmp_path / 'home-symlink'
    monkeypatch.setenv('HOME', str(home))
    target = tmp_path / 'real-target.key'
    target.write_text(CLIENT_KEY_SENTINEL + '\n', encoding='utf-8')
    target.chmod(0o600)
    key_dir = home / '.config' / 'plangraph'
    key_dir.mkdir(parents=True)
    key_dir.chmod(0o700)
    key_path = key_dir / 'omniroute-client.key'
    key_path.symlink_to(target)

    with caplog.at_level(logging.WARNING):
        assert host_keys._gateway_key({}) is None
        env = {}
        add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True)
        assert env == {}

    assert len(caplog.records) == 2
    for rec in caplog.records:
        assert str(key_path) in rec.message
        assert 'mode=' in rec.message
        assert CLIENT_KEY_SENTINEL not in rec.message
    captured = capsys.readouterr()
    assert CLIENT_KEY_SENTINEL not in captured.out + captured.err


@pytest.mark.skipif(os.name == 'nt' or not hasattr(os, 'geteuid'), reason='POSIX euid')
def test_client_key_wrong_owner_refused_with_warning(tmp_path, monkeypatch, fake_autoos, caplog, capsys):
    home = tmp_path / 'home-owner'
    monkeypatch.setenv('HOME', str(home))
    _, key_path = _write_client_key(home, CLIENT_KEY_SENTINEL + '\n')
    monkeypatch.setattr(os, 'geteuid', lambda: os.getuid() + 1)

    with caplog.at_level(logging.WARNING):
        assert host_keys._gateway_key({}) is None

    assert len(caplog.records) == 1
    assert str(key_path) in caplog.records[0].message
    assert '0600' in caplog.records[0].message
    assert CLIENT_KEY_SENTINEL not in caplog.text
    captured = capsys.readouterr()
    assert CLIENT_KEY_SENTINEL not in captured.out + captured.err


def test_client_key_absent_falls_back_to_autoos(tmp_path, monkeypatch, fake_autoos, caplog):
    home = tmp_path / 'home-absent'
    (home / '.config' / 'plangraph').mkdir(parents=True)
    (home / '.config' / 'plangraph').chmod(0o755)
    monkeypatch.setenv('HOME', str(home))

    with caplog.at_level(logging.WARNING):
        assert host_keys._gateway_key({}) == GATEWAY_FAKE
        env = {}
        add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True)
        assert env == {GATEWAY_KEY_ENV: GATEWAY_FAKE}
    assert not caplog.records


@pytest.mark.parametrize('content', ['', '   \n\t  '])
def test_client_key_empty_file_means_no_key_without_fallback(tmp_path, monkeypatch, fake_autoos, content):
    home = tmp_path / 'home-empty'
    monkeypatch.setenv('HOME', str(home))
    _write_client_key(home, content)

    assert host_keys._gateway_key({}) is None
    env = {}
    add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True)
    assert env == {}


@pytest.mark.parametrize('content', [
    CLIENT_KEY_SENTINEL + '\nsecond-line\n',
    (CLIENT_KEY_SENTINEL + '-').encode('utf-8') + b'\xff',
    (CLIENT_KEY_SENTINEL + '-' + 'x' * 9000).encode('utf-8'),
])
def test_client_key_invalid_content_never_leaks_value(tmp_path, monkeypatch, fake_autoos, caplog, capsys, content):
    home = tmp_path / 'home-invalid'
    monkeypatch.setenv('HOME', str(home))
    _, key_path = _write_client_key(home, content)

    with caplog.at_level(logging.WARNING):
        assert host_keys._gateway_key({}) is None

    assert len(caplog.records) == 1
    assert str(key_path) in caplog.records[0].message
    assert '0600' in caplog.records[0].message
    assert CLIENT_KEY_SENTINEL not in caplog.text
    captured = capsys.readouterr()
    assert CLIENT_KEY_SENTINEL not in captured.out + captured.err


def test_client_key_callers_use_real_reader(tmp_path, monkeypatch):
    from plangraph.routing_support import omniroute_state, quota_snapshots

    home = tmp_path / 'home-callers'
    monkeypatch.setenv('HOME', str(home))
    _write_client_key(home, CLIENT_KEY_SENTINEL + '\n')

    seen_auth = []

    def fake_fetch(url, headers, timeout):
        seen_auth.append(headers.get('Authorization'))
        return 200, b'{"connections": [{"provider": "p", "isActive": true}], "data": []}'

    env = {'OMNIROUTE_BASE_URL': 'https://gw.example/v1'}
    state = omniroute_state.from_env(env, fetch=fake_fetch)
    assert state.provider_state('p').available is True
    assert quota_snapshots.fetch_snapshot_rows(env, fetch=fake_fetch) == []
    assert seen_auth == ['Bearer ' + CLIENT_KEY_SENTINEL, 'Bearer ' + CLIENT_KEY_SENTINEL]
