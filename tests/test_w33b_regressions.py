import json
import subprocess
from types import SimpleNamespace

import pytest
from plangraph.spawner import resolver as r, opencode_overlay as o
from plangraph.spawner import cli


def test_unterminated_drain_bounded_and_boundary_secret(monkeypatch):
    secret = 'bounda' 'ry-private-value'
    monkeypatch.setenv('TEST_SECRET', secret)
    chunks = iter([b'x' * (65536 - 10) + secret[:10].encode(), secret[10:].encode()] + [b'x' * 8192] * 1024)
    output, sizes = [], []
    cli._drain_chunks(chunks, output.append, sizes.append)
    assert max(sizes) <= 128 * 1024
    assert secret.encode() not in b''.join(output)
    assert secret[10:].encode() not in b''.join(output)


def test_pruning_preserves_recent_and_live(tmp_path):
    import os
    import time
    paths = []
    for i in range(24):
        path = tmp_path / f'supervisor-{i:02}.stderr.log'
        path.touch()
        paths.append(path)
    old, live = paths[:2]
    for path in (old, live):
        os.utime(path, (time.time() - 90000,) * 2)
    live.with_suffix(live.suffix + '.pid').write_text(str(os.getpid()))
    cli._prune_supervisor_logs(tmp_path)
    assert not old.exists()
    assert live.exists()
    assert all(p.exists() for p in paths[2:])


@pytest.mark.real_probe
@pytest.mark.parametrize('stdout', ['', 'No models found\n'])
def test_empty_success_is_unknown_and_admitted(monkeypatch, stdout):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: calls.append(kw) or SimpleNamespace(returncode=0, stdout=stdout))
    assert r.host_skip_reason(dict(harness='opencode', model='test/model')) is None
    assert r.served_models('{}') is None


def test_empty_success_cached_for_thirty_seconds(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: calls.append(kw) or SimpleNamespace(returncode=0, stdout=''))
    for now in (0, 29):
        assert r._served_models_real('{}', clock=lambda: now) is None
    assert len(calls) == 1
    assert r._served_models_real('{}', clock=lambda: 31) is None
    assert len(calls) == 2


@pytest.mark.real_probe
def test_extra_credentials_reach_probe(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: seen.append(kw['env']) or SimpleNamespace(returncode=0, stdout='test/model'))
    cfg = json.dumps({'providers': {'test': {'env': ['MY_GATEWAY_TOKEN']}}})
    r.host_skip_reason(dict(harness='opencode', model='test/model'), env={'MY_GATEWAY_TOKEN': 'test-only', 'OPENCODE_CONFIG_CONTENT': cfg})
    assert 'MY_GATEWAY_TOKEN' in seen[0]


@pytest.mark.parametrize('requested,listed,admitted', [
    ('test/model#xhigh', 'test/model#high', False),
    ('test/model#HIGH', 'test/model#high', True),
    ('test/model', 'test/model#high', True),
    ('test/model#high', 'test/model', True),
])
def test_variant_admission(requested, listed, admitted):
    reason = r.host_skip_reason(dict(harness='opencode', model=requested), probe=lambda e: {listed})
    assert (reason is None) == admitted


def test_root_generic_precedence_and_gateway_exception(monkeypatch):
    root = {'options': {'baseURL': 'https://real', 'other': 'root'}, 'settings': {'baseURL': 'https://real'}, 'headers': {'keep': 'root', 'run': 'old'}, 'custom': 'root'}
    monkeypatch.setattr(o, 'load_root_providers', lambda: {'omniroute': json.loads(json.dumps(root))})
    generated = {'options': {'baseURL': 'http://localhost:20128/v1', 'other': 'generated'}, 'settings': {'baseURL': 'http://localhost:20128/v1'}, 'headers': {'run': 'new'}, 'custom': 'generated'}
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps({'providers': {'omniroute': generated}})}
    def entry(e):
        return json.loads(o.overlay_env(e)['OPENCODE_CONFIG_CONTENT'])['providers']['omniroute']
    result = entry(env)
    assert result['options'] == root['options']
    assert result['settings'] == root['settings']
    assert result['custom'] == 'root'
    assert result['headers'] == {'keep': 'root', 'run': 'new'}
    result = entry(dict(env, OMNIROUTE_BASE_URL='http://localhost:20128/v1'))
    assert result['options']['baseURL'] == result['settings']['baseURL'] == 'http://localhost:20128/v1'


def test_invalid_caller_overlay_warns_unknown(monkeypatch, caplog):
    monkeypatch.setattr(o, 'load_root_providers', lambda: {})
    monkeypatch.setattr(o, '_content_warned', False, raising=False)
    for _ in range(2):
        assert o.overlay_env({'OPENCODE_CONFIG_CONTENT': '{private-invalid'})[o.OVERLAY_UNKNOWN]
    assert len(caplog.records) == 1
    assert 'private-invalid' not in caplog.text
