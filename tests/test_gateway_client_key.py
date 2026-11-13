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
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_KEY_FILE', raising=False)
    path = tmp_path / '.config/plangraph/omniroute-client.key'
    path.parent.mkdir(parents=True)
    path.write_text(SENTINEL + '\r\n'); path.chmod(0o600)
    return path


def test_client_file_resolved_at_spawn_time(key_file, monkeypatch, tmp_path):
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


def test_non_gateway_model_needs_no_gateway_key(key_file):
    key_file.unlink()
    job = asyncio.run(SpawnerRunner().spawn('fixture', 'opencode-go/gemini-4-argon', dry_run=True))
    assert not any(e.get('event') == 'render_refused' for e in job.events)


@pytest.mark.parametrize('kind', ['missing', 'empty', 'blank', 'unreadable', 'directory', 'writable', 'mode-zero'])
@pytest.mark.parametrize('model', ['mimo/mimo-v2.6-pro', 'omniroute/cx-gpt-6.1-sol'])
def test_absent_file_skips_without_leaking(key_file, monkeypatch, tmp_path, capsys, caplog, kind, model):
    if kind in ('missing', 'directory'):
        key_file.unlink()
        if kind == 'directory': key_file.mkdir()
    elif kind in ('empty', 'blank'):
        key_file.write_text('' if kind == 'empty' else '\r\n')
    elif kind in ('writable', 'mode-zero'):
        key_file.chmod(0o666 if kind == 'writable' else 0o000)
    else:
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == key_file: raise PermissionError(SENTINEL)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', denied)
    env = {}
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
    for path in (ROOT / 'src').rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            assert not any(name in path.read_bytes() for name in
                (b'AUTOOS_OMNIROUTE_KEY', b'autoos_gateway_key', b'api-keys.yml')), path


def test_client_file_documented():
    text = (ROOT / 'docs/SPAWNER.md').read_text()
    assert all(word in text for word in ('~/.config/plangraph/omniroute-client.key', '0600',
                                         'PLANGRAPH_OMNIROUTE_KEY_FILE'))
    assert len((ROOT / 'changelog.d/GATEWAY-CLIENT-KEY-PLANGRAPH-FILE.md').read_text().splitlines()) == 1
