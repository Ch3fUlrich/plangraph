import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_private_atomic_creation(tmp_path, monkeypatch):
    from plangraph.spawner.private_io import atomic_write_private
    import tempfile
    original = tempfile.mkstemp
    names = []
    def hooked(*args, **kwargs):
        fd, name = original(*args, **kwargs)
        assert os.stat(name).st_mode & 0o777 == 0o600
        names.append(name)
        return fd, name
    monkeypatch.setattr(tempfile, 'mkstemp', hooked)
    old = os.umask(0o022)
    try:
        path = tmp_path / 'record.json'
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda n: atomic_write_private(path, json.dumps({'n': n})), range(2)))
        assert json.loads(path.read_text())['n'] in (0, 1)
        assert len(set(names)) == 2
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_mimo_segment_boundaries():
    from plangraph.mimo_policy import is_mimo_id
    assert not is_mimo_id('foo/mimodev')
    for model in ('omniroute/mimo-v2.6-pro', 'opencode/mimo-v2.6-flash',
                  'mimo-v2.6-pro', 'foo/mimo-v2.6-pro', 'omniroute-mimo/other'):
        assert is_mimo_id(model)


def test_pure_doc_exemption():
    from plangraph.spawner.risk import evaluate_risk
    assert evaluate_risk(['docs/auth.md'], bucket='S0', is_pure_doc=True).reviewer_count == 0
    result = evaluate_risk(['docs/auth.md'])
    assert result.risk == 'normal' and result.reviewer_count == 1


def test_cli_bench_required(tmp_path, capsys):
    from plangraph.spawner.cli import main
    assert main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":100}',
                 '--require-bench']) == 3
    assert 'benchmark rows missing' in capsys.readouterr().err


def test_mcp_bench_required(tmp_path):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path)
    result = asyncio.run(server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'plangraph_agent_route', 'arguments': {
            'card': {'estimate_tokens': 100}, 'require_bench': True}}}))
    assert 'benchmark rows missing' in result['error']['message']


def test_nonce_header():
    from plangraph.spawner.clients import build_routing_envelope_headers
    assert build_routing_envelope_headers(nonce='unique')['X-OmniRoute-Run-Nonce'] == 'unique'


@pytest.mark.parametrize('verb', ['route', 'spawn'])
def test_benchmark_entry_roundtrips(tmp_path, capsys, monkeypatch, verb):
    from plangraph.spawner.cli import main
    from plangraph.spawner.mcp import MCPSpawnerServer
    from plangraph.planner.legs import DEFAULT_LEGS
    import sqlite3
    path = tmp_path / 'bench.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE catalog_benchmarks (model_id TEXT, aa_intelligence REAL)')
        db.executemany('INSERT INTO catalog_benchmarks VALUES (?, 50)', [(l.model,) for l in DEFAULT_LEGS])
    # implement keeps operator writer order (D-2683); review shows the benchmark ranking
    args = ['--state', str(tmp_path), verb, '--card', '{"estimate_tokens":100}', '--role', 'review',
            '--require-bench', '--bench-db', str(path)]
    if verb == 'spawn':
        args += ['task', '--dry-run']
    assert main(args) == 0
    assert 'benchmark' in capsys.readouterr().out
    server = MCPSpawnerServer(state=tmp_path)
    arguments = dict(card={'estimate_tokens': 100}, prompt='task', dry_run=True,
                     require_bench=True, bench_db=str(path))
    def call():
        return asyncio.run(server.handle_rpc_request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'plangraph_agent_' + verb, 'arguments': arguments}}))
    assert call()['result']['isError'] is False
    monkeypatch.setenv('PLANGRAPH_REQUIRE_BENCH', '1')
    arguments.pop('bench_db')
    arguments.pop('require_bench')
    assert 'benchmark rows missing' in call()['error']['message']
    assert main(['--state', str(tmp_path), verb, '--card', '{"estimate_tokens":100}'] +
                (['task', '--dry-run'] if verb == 'spawn' else [])) == 3


@pytest.mark.parametrize('nonce_kind', ['missing', 'wrong', 'valid'])
def test_envelope_nonce(tmp_path, nonce_kind):
    from plangraph.spawner.runner import SpawnerRunner
    async def run():
        calls = []
        runner = SpawnerRunner()
        runner.provider_response = lambda *a, **kw: calls.append((a, kw))
        class Process:
            returncode = 0
            async def communicate(self):
                response = dict(leg='cx', status=429, body='quota', headers={})
                if nonce_kind != 'missing':
                    response['nonce'] = next(iter(runner._nonces.values())) if nonce_kind == 'valid' else 'wrong'
                return b'', json.dumps({'autoos_provider_response': response}).encode()
        async def factory(*args, **kwargs):
            return Process()
        runner.process_factory = factory
        job = await runner.spawn('test', extra_env={'PLANGRAPH_GATEWAY_URL': 'http://127.0.0.1:20128/v1'}, wait_seconds=3)
        assert len(calls) == (1 if nonce_kind == 'valid' else 0)
        assert [e for e in job.events if e['event'] != 'admitted_by_render'] == ([] if nonce_kind == 'valid' else [{'event': 'forged_envelope'}])
    asyncio.run(run())
