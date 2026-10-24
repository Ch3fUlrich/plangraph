"""Round-four failure contracts."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import JobInfo, SpawnerRunner, extract_served_telemetry


def test_closing_stdin_refuses_steer():
    runner = SpawnerRunner()
    runner.jobs['one'] = JobInfo('one', 'running', '', '')
    runner._processes['one'] = SimpleNamespace(stdin=SimpleNamespace(is_closing=lambda: True))
    assert asyncio.run(runner.steer('one', 'hello')) == {
        'ok': False, 'steered': False, 'error': 'stdin closing'}


def test_missing_served_model_is_unknown():
    assert not extract_served_telemetry('', fallback_model='test/model')[0]


def test_provider_response_requires_authentication():
    runner = SpawnerRunner()
    runner.jobs['one'] = JobInfo('one', 'running', '', '')
    with pytest.raises((ValueError, TypeError)):
        runner.provider_response('one', 'test/model', 200, '', {})


def test_card_cannot_self_assert_exemption():
    from plangraph.planner.routing import plan_spawn, SpawnRefused
    with pytest.raises(SpawnRefused):
        plan_spawn({'estimate_tokens': 1, 'reviewer_count': 0,
                    'review_exempt': True}, role='implement')


def test_bench_data_error_is_distinct(tmp_path):
    from plangraph.spawner.bench_entry import benchmark_options
    with pytest.raises(Exception) as error:
        benchmark_options(True, str(tmp_path / 'missing.db'))
    assert type(error.value).__name__ == 'BenchDataError'


def test_failed_fanout_is_unknown(tmp_path):
    from plangraph.spawner.measure import grep_fanout
    assert grep_fanout(tmp_path, 'unique_symbol') is None


@pytest.mark.parametrize('suffix,expected', [('2', True), ('v2.6', True),
    ('-v2.6flash', True), ('dev', False), ('sa', False)])
def test_glued_policy(suffix, expected):
    from plangraph.mimo_policy import is_mimo_id
    assert is_mimo_id('mimo' + suffix) is expected


def test_sqlite_private_at_connect(tmp_path, monkeypatch):
    import sqlite3
    from plangraph.spawner.catalog_db import CatalogDB
    connect = sqlite3.connect
    path = tmp_path / 'catalog.db'
    def checked(name, **kwargs):
        assert path.stat().st_mode & 0o777 == 0o600
        return connect(name, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', checked)
    with CatalogDB(path):
        pass
    path.chmod(0o644)
    with CatalogDB(path):
        pass


def test_scanner_real_scope(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('sweep', Path('tools/mimo_path_sweep.py'))
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    # These are the sweep's own on-disk fixtures, not hidden policy examples.
    text = '''{
      "providers": {
        "mimo": { /* a multiline comment
          } } {
        */ "settings": {"text": "{"}, "models": {
          "mimo-v2.6-flash": {}
        }},
        "omniroute": {"models": {
          "mimo-v2.6-flash": {}
        }}
      },
      "mimo-v2.6-pro": {}
    }'''
    (tmp_path / 'config.jsonc').write_text(text)
    hits = list(sweep.scan(tmp_path, []))
    assert [hit['line'] for hit in hits] == [9, 12]


def test_config_limits_match_seed():
    import json
    from pathlib import Path
    from plangraph.spawner.catalog_seed import SEED_MODELS
    text = Path('opencode.jsonc').read_text()
    # Config only uses full-line comments.
    config = json.loads('\n'.join(line for line in text.splitlines()
                                  if not line.lstrip().startswith('//')))
    models = config['providers']['mimo']['models']
    for row in SEED_MODELS:
        if row['model_id'] in ('mimo/mimo-v2.6-flash', 'mimo/mimo-v2.6-pro'):
            assert models[row['model_id'].split('/')[1]]['limit']['context'] == row['context_tokens']


def test_adapter_and_nonce_authentication():
    runner = SpawnerRunner()
    runner.jobs['one'] = JobInfo('one', 'running', '', '')
    runner._adapter_tokens['one'] = object()
    runner._nonces['one'] = 'private'
    marks = []
    store = SimpleNamespace(record_probe_pass=lambda *a, **kw: marks.append(a))
    for source, kwargs in [('unknown', {}), ('gateway_adapter', {'adapter_token': object()}),
                           ('stderr_envelope', {'nonce': 'wrong'})]:
        with pytest.raises(ValueError):
            runner.provider_response('one', 'test/model', 200, '', {}, source=source, store=store, **kwargs)
    assert marks == []
    runner.provider_response('one', 'test/model', 200, '', {}, source='gateway_adapter',
                             adapter_token=runner._adapter_tokens['one'], store=store)
    assert marks == [('test/model',)]


def test_bench_error_public_entries(tmp_path, capsys):
    from plangraph.spawner.cli import main
    from plangraph.spawner.mcp import MCPSpawnerServer
    path = str(tmp_path / 'missing.db')
    assert main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":1}',
                 '--bench-db', path, '--require-bench']) == 3
    assert 'cannot read' in capsys.readouterr().err
    response = asyncio.run(MCPSpawnerServer(state=tmp_path).handle_rpc_request({
        'id': 7, 'method': 'tools/call', 'params': {'name': 'plangraph_agent_route',
        'arguments': {'card': {'estimate_tokens': 1}, 'require_bench': True, 'bench_db': path}}}))
    assert response['error']['data']['type'] == 'BenchDataError'
    assert response['error']['data']['legs'] == []


def test_resolver_ignores_claimed_exemption():
    from plangraph.spawner.resolver import resolve_review_plan
    with pytest.raises(ValueError, match='zero reviewers'):
        resolve_review_plan('test/model', reviewer_count=0, review_exempt=True)


def test_nonce_orphans_evicted():
    runner = SpawnerRunner()
    runner._nonces = {str(i): str(i) for i in range(4096)}
    asyncio.run(runner.spawn('task', 'mimo/mimo-v2.6-flash', dry_run=True))
    assert runner._nonces == {}


def test_high_card_cannot_request_one_reviewer():
    from plangraph.planner.routing import plan_spawn, SpawnRefused
    with pytest.raises(SpawnRefused, match='two reviewers'):
        plan_spawn({'estimate_tokens': 1, 'risk': 'critical', 'reviewer_count': 1},
                   role='implement')
