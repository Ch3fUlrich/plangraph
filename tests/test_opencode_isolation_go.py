"""D-4196 provider-only auth under review isolation; no host service needed."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner import resolver
from plangraph.spawner.opencode_isolation import install_go_credential
from plangraph.spawner.runner import SpawnerRunner, _opencode_tools_denied
from fake_pid import FAKE_PID


def source_store(path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE migration(id TEXT PRIMARY KEY, time_completed INTEGER)')
        db.execute('CREATE TABLE credential(id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, '
                   'value TEXT, connector_id TEXT, method_id TEXT, active INTEGER, '
                   'time_created INTEGER, time_updated INTEGER)')
        for provider in ('opencode-go', 'other'):
            db.execute('INSERT INTO credential VALUES(?,?,?,?,?,?,?,?,?)',
                       (provider, provider, 'test', 'synthetic-private-value', None, None, 1, 0, 0))


@pytest.mark.usefixtures('fake_ownership')
@pytest.mark.parametrize('present', [True, False])
def test_review_provider_only(tmp_path, monkeypatch, present):
    source = tmp_path / 'source.db'
    if present:
        source_store(source)
    monkeypatch.setenv('PLANGRAPH_OPENCODE_GO_AUTH_DB', str(source))
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    seen = []
    async def factory(*args, **kwargs):
        env = kwargs['env']
        seen.append(dict(env))
        assert env['HOME'] == env['XDG_DATA_HOME'] == env['OPENCODE_CONFIG_DIR']
        assert _opencode_tools_denied(env['OPENCODE_CONFIG_CONTENT'], env)
        cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        assert cfg['mcp'] == {} and cfg['plugin'] == []
        path = Path(env['XDG_DATA_HOME']) / 'opencode' / 'opencode.db'
        assert path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT integration_id FROM credential').fetchall() == [('opencode-go',)]
        async def communicate():
            return b'done', b''
        return SimpleNamespace(pid=FAKE_PID, returncode=0, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    async def run():
        job = await runner.spawn('review', model_or_combo='opencode-go/glm-5.2',
                                 role='review', client='opencode', wait_seconds=3)
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]
        return job
    job = asyncio.run(run())
    assert bool(seen) == present
    assert 'synthetic-private-value' not in repr(seen) + repr(job.events)
    if present:
        assert not Path(seen[0]['HOME']).exists()
    else:
        assert 'not_served' in job.stderr


def test_missing_store_is_clean(tmp_path):
    assert not install_go_credential(tmp_path / 'job', tmp_path / 'absent')


def test_disabled_and_fallback_routes():
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import plan_spawn
    card = {'estimate_tokens': 1000, 'risk_tags': []}
    plans = [plan_spawn(card, role=role, writer_families=('claude',))
             for role in ('review', 'implement')]
    for leg in DEFAULT_LEGS:
        if leg.model.startswith('opencode/') and leg.model.endswith('-free'):
            assert 'D-4196' in leg.disabled
            for plan in plans:
                assert leg.id not in [l.id for l in plan.legs]
                assert any(s['leg'] == leg.id and 'D-4196' in s['reason'] for s in plan.skipped)
    assert 'omniroute-gemini-3.8-flash' in [l.id for l in plans[0].legs]
    assert 'omniroute-gemini-3.8-flash' not in [l.id for l in plans[1].legs]
