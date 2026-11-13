"""Hermetic client-file and spawn refusal contract; no gateway calls."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from plangraph.routing_support import host_keys
from plangraph.spawner.runner import SpawnerRunner, spawn_service

SENTINEL = 'fixture-client-secret-731'
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for name in ('AUTOOS_ROOT', 'PLANGRAPH_OMNIROUTE_KEY_FILE', 'PLANGRAPH_OMNIROUTE_KEY',
                 'PLANGRAPH_GATEWAY_URL', 'OMNIROUTE_BASE_URL', 'AUTOOS_OMNIROUTE_URL'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / '.config/plangraph/omniroute-client.key'
    path.parent.mkdir(parents=True)
    path.write_text(SENTINEL + '\r\n'); path.chmod(0o600)
    return path


def test_client_file_resolved_at_spawn_time(key_file, monkeypatch, tmp_path):
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert host_keys._gateway_key({}) == SENTINEL
    override = tmp_path / 'override.key'
    override.write_text('different-fixture\n'); override.chmod(0o600)
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_KEY_FILE', str(override))
    assert host_keys._gateway_key({}) == 'different-fixture'
    env = {'PLANGRAPH_OMNIROUTE_KEY_FILE': str(key_file)}
    assert host_keys.add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True) is None
    assert env['PLANGRAPH_OMNIROUTE_KEY'] == SENTINEL
    key_file.write_text('rotated-fixture\n')
    assert host_keys._gateway_key(env) == 'rotated-fixture'


def test_non_gateway_model_needs_no_gateway_key(key_file, monkeypatch):
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', json.dumps({'providers': {
        'mimo': {'env': ['PLANGRAPH_OMNIROUTE_KEY']}}}))
    captured = {}
    original = host_keys.add_host_keys
    def spy(env, model, *, needs_gateway):
        result = original(env, model, needs_gateway=needs_gateway)
        captured.update(env=env.copy(), needs_gateway=needs_gateway)
        return result
    monkeypatch.setattr(host_keys, 'add_host_keys', spy)
    job = asyncio.run(SpawnerRunner().spawn('fixture', 'opencode-go/gemini-4-argon', dry_run=True))
    assert not any(e.get('event') == 'render_refused' for e in job.events)
    assert captured['needs_gateway'] is False and 'PLANGRAPH_OMNIROUTE_KEY' not in captured['env']


@pytest.mark.parametrize('kind', ['missing', 'empty', 'blank', 'unreadable', 'directory', 'writable', 'group-writable', 'mode-zero', 'owner-unreadable'])
@pytest.mark.parametrize('model', ['mimo/mimo-v2.6-pro', 'omniroute/cx-gpt-6.1-sol'])
def test_absent_file_skips_without_leaking(key_file, monkeypatch, tmp_path, capsys, caplog, kind, model):
    monkeypatch.setenv('PLANGRAPH_GATEWAY_URL', 'http://127.0.0.1:9/v1')
    if kind in ('missing', 'directory'):
        key_file.unlink()
        if kind == 'directory': key_file.mkdir()
    elif kind in ('empty', 'blank'):
        key_file.write_text('' if kind == 'empty' else '\r\n')
    elif kind in ('writable', 'group-writable', 'mode-zero', 'owner-unreadable'):
        key_file.chmod({'writable': 0o602, 'group-writable': 0o620, 'mode-zero': 0o000, 'owner-unreadable': 0o044}[kind])
    else:
        original = Path.open
        def denied(path, *args, **kwargs):
            if path == key_file: raise PermissionError(SENTINEL)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', denied)
    env = {'PLANGRAPH_OMNIROUTE_KEY_FILE': str(key_file)}
    assert host_keys.add_host_keys(env, 'mimo/mimo-v2.6-pro', needs_gateway=True) == 'gateway_key_absent'
    assert 'PLANGRAPH_OMNIROUTE_KEY' not in env
    assert host_keys._gateway_key({}) is None
    factory = AsyncMock()
    service = spawn_service(SpawnerRunner(process_factory=factory), tmp_path / 'runs')
    record = asyncio.run(service.spawn(card={'estimate_tokens': 1000}, prompt='fixture',
        model=model, override_reason='hermetic key refusal check', wait_seconds=None))
    assert factory.call_count == 0
    assert record['attempts'][0]['skip_reason'] == 'gateway_key_absent'
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err + caplog.text + json.dumps(record)
    assert SENTINEL not in service._path(record['run_id']).read_text()


def test_no_legacy_gateway_sources_in_src():
    for folder in ('src', 'tools'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                assert not any(name in path.read_bytes() for name in
                    (b'AUTOOS_OMNIROUTE_KEY', b'autoos_gateway_key', b'api-keys.yml')), path
                if folder == 'src':
                    assert b'code/AutoOS' not in path.read_bytes(), path
    assert all(json.loads((ROOT / 'configuration/opencode/providers.json').read_text())['providers'][name]['env']
               == ['PLANGRAPH_OMNIROUTE_KEY'] for name in ('mimo', 'omniroute'))


def test_client_file_documented():
    text = (ROOT / 'docs/SPAWNER.md').read_text()
    assert all(word in text for word in ('~/.config/plangraph/omniroute-client.key', '0600',
                                         'PLANGRAPH_OMNIROUTE_KEY_FILE', 'group- or world-writable files are refused',
                                         'any group/other permission bits', '~/.config/autoos/api_keys.conf'))
    assert '~/.config/autoos/api_keys.conf' in (ROOT / 'changelog.d/GATEWAY-CLIENT-KEY-PLANGRAPH-FILE.md').read_text()
    assert len((ROOT / 'changelog.d/GATEWAY-CLIENT-KEY-PLANGRAPH-FILE.md').read_text().splitlines()) == 1


def test_spawn_contract_docstrings_match_both_cases():
    import test_spawn_win_render as spawn_tests
    assert 'D-5755' in spawn_tests.test_parent_env_gateway_key_passthrough_and_no_leak.__doc__
    assert all(reason in spawn_tests.test_unresolved_refusal_falls_through_to_next_leg.__doc__
               for reason in ('not_served_on_host', 'gateway_key_absent'))

@pytest.mark.parametrize('mode', [0o640, 0o644, 0o620, 0o602, 0o610, 0o604, 0o601, 0o044])
def test_group_or_world_writable_files_are_refused(key_file, mode):
    """Refuse writable files and every other group/other permission bit."""
    key_file.chmod(mode)
    assert host_keys._gateway_key({}) is None
