"""D-4196 provider-only auth under review isolation; no host service needed."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner import resolver
from plangraph.spawner.opencode_isolation import (
    GO_ABSENT_REASON, GO_AUTH_DB_ENV, install_go_credential,
)
from plangraph.spawner.runner import SpawnerRunner, _opencode_tools_denied
from fake_pid import FAKE_PID


def source_store(path, active=1):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE migration(id TEXT PRIMARY KEY, time_completed INTEGER)')
        db.execute('CREATE TABLE credential(id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, '
                   'value TEXT, connector_id TEXT, method_id TEXT, active INTEGER, '
                   'time_created INTEGER, time_updated INTEGER)')
        for provider in ('opencode-go', 'other'):
            db.execute('INSERT INTO credential VALUES(?,?,?,?,?,?,?,?,?)',
                       (provider, provider, 'test', 'synthetic-private-value', None, None, active, 0, 0))


@pytest.mark.usefixtures('fake_ownership')
@pytest.mark.parametrize('present,active', [(True, 1), (True, None), (True, 0), (False, None)],
                         ids=['active', 'null', 'inactive', 'missing'])
@pytest.mark.parametrize('model', ['opencode-go/gemini-4-argon', 'opencode-go-gemini-4-argon',
                                   'go-test-combo', 'aliases/go-test-combo'])
def test_review_provider_only(tmp_path, monkeypatch, caplog, present, active, model):
    source = tmp_path / 'source.db'
    if present:
        source_store(source, active)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    if model in ('go-test-combo', 'aliases/go-test-combo'):
        monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
            {'id': 'opencode-go/gemini-4-argon', 'combos': [model]}])
        monkeypatch.setattr(resolver, 'catalog_row_admitted', lambda row: True)
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
        async def communicate(input=None):
            return b'done', b''
        return SimpleNamespace(pid=FAKE_PID, returncode=0, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    async def run():
        job = await runner.spawn('review', model_or_combo=model,
                                 role='review', client='opencode', wait_seconds=3)
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]
        return job
    job = asyncio.run(run())
    usable = present and active != 0
    assert bool(seen) == usable
    assert 'synthetic-private-value' not in repr(seen) + repr(job.events) + job.stderr + caplog.text
    if usable:
        assert not Path(seen[0]['HOME']).exists()
    else:
        assert GO_ABSENT_REASON in job.stderr


@pytest.mark.parametrize('active', [1, None, 0], ids=['active', 'null', 'inactive'])
def test_credential_selection_excludes_inactive_and_other_providers(tmp_path, active):
    source = tmp_path / 'source.db'
    source_store(source, active)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE credential SET active = NULL WHERE integration_id = 'other'")
        db.execute('INSERT INTO credential VALUES(?,?,?,?,?,?,?,?,?)',
                   ('inactive-go', 'opencode-go', 'test', 'synthetic-inactive-value',
                    None, None, 0, 0, 0))
    job = tmp_path / 'job'
    assert install_go_credential(job, source) is (active != 0)
    destination = job / 'opencode' / 'opencode.db'
    if active == 0:
        assert not destination.exists()
    else:
        with sqlite3.connect(destination) as db:
            assert db.execute('SELECT id, integration_id, active FROM credential').fetchall() == [
                ('opencode-go', 'opencode-go', active)]
    with sqlite3.connect(source) as db:
        assert db.execute('SELECT id, active FROM credential ORDER BY id').fetchall() == [
            ('inactive-go', 0), ('opencode-go', active), ('other', None)]


@pytest.mark.parametrize('inactive', [False, True], ids=['no-row', 'inactive'])
def test_refusal_names_credential_query_not_login(tmp_path, monkeypatch, inactive):
    from plangraph.spawner.opencode_isolation import review_credential_reason
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))
    child_env = {GO_AUTH_DB_ENV: str(source)}
    usable_job = tmp_path / 'usable-job'
    # Prove the fixture store is read before testing either refusal result.
    assert review_credential_reason('opencode-go/test-model', str(usable_job), child_env) is None
    with sqlite3.connect(usable_job / 'opencode' / 'opencode.db') as db:
        assert db.execute('SELECT id, active FROM credential').fetchall() == [('opencode-go', 1)]
    with sqlite3.connect(source) as db:
        if inactive:
            db.execute("UPDATE credential SET active = 0 WHERE integration_id = 'opencode-go'")
        else:
            db.execute("DELETE FROM credential WHERE integration_id = 'opencode-go'")
    refused_job = tmp_path / 'refused-job'
    reason = review_credential_reason('opencode-go/test-model', str(refused_job), child_env)
    assert reason == GO_ABSENT_REASON
    assert 'query' in reason and 'no row' in reason and 'inactive' in reason
    assert 'login' not in reason.lower() and 'log in' not in reason.lower()
    assert child_env['XDG_DATA_HOME'] == str(refused_job)
    assert not (refused_job / 'opencode' / 'opencode.db').exists()


@pytest.mark.parametrize('reverse', [False, True], ids=['forward', 'reverse'])
@pytest.mark.parametrize('legacy_times,v2_times,expected', [
    ((1, 3), (2, 2), 'legacy-go'),
    ((2, 2), (1, 3), 'v2-go'),
    ((1, 3), (2, 3), 'v2-go'),
    ((2, 3), (1, 3), 'legacy-go'),
    ((2, 3), (2, 3), 'legacy-go'),
], ids=['legacy-updated', 'v2-updated', 'v2-created', 'legacy-created', 'id-tie'])
def test_mixed_version_store_selects_one_deterministic_credential(
        tmp_path, reverse, legacy_times, v2_times, expected):
    source = tmp_path / 'source.db'
    source_store(source, active=0)
    rows = [
        ('legacy-go', 'opencode-go', 'test', 'synthetic-legacy-value', None, None,
         1, *legacy_times),
        ('v2-go', 'opencode-go', 'test', 'synthetic-v2-value', None, None,
         None, *v2_times),
    ]
    with sqlite3.connect(source) as db:
        # Newer inactive Go and NULL other-provider rows must not win.
        db.execute('UPDATE credential SET time_created = 99, time_updated = 99')
        db.execute("UPDATE credential SET active = NULL WHERE integration_id = 'other'")
        db.executemany('INSERT INTO credential VALUES(?,?,?,?,?,?,?,?,?)',
                       list(reversed(rows)) if reverse else rows)
    before = source.read_bytes()
    job = tmp_path / 'job'
    assert install_go_credential(job, source)
    destination = job / 'opencode' / 'opencode.db'
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as db:
        assert db.execute('SELECT * FROM credential').fetchall() == [
            next(row for row in rows if row[0] == expected)]
    assert source.read_bytes() == before


def test_missing_store_is_clean(tmp_path):
    assert not install_go_credential(tmp_path / 'job', tmp_path / 'absent')


def test_full_schema_is_rebuilt_without_application_data(tmp_path):
    source = tmp_path / 'source.db'
    source_store(source)
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE session(id INTEGER PRIMARY KEY AUTOINCREMENT, secret TEXT)')
        db.execute('CREATE VIRTUAL TABLE search USING fts5(content)')
        db.execute('CREATE INDEX credential_provider ON credential(integration_id)')
        db.execute('INSERT INTO session(secret) VALUES(?)', ('synthetic-session-secret',))
    job = tmp_path / 'job'
    assert install_go_credential(job, source)
    with sqlite3.connect(job / 'opencode' / 'opencode.db') as db:
        names = {r[0] for r in db.execute('SELECT name FROM sqlite_master')}
        assert 'credential_provider' in names
        assert {'session', 'search'} <= names
        assert db.execute('SELECT count(*) FROM session').fetchone() == (0,)


def test_credential_store_does_not_claim_host_migrations(tmp_path):
    source = tmp_path / 'source.db'
    source_store(source)
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE session(id TEXT PRIMARY KEY)')
        db.execute('INSERT INTO migration VALUES(?, ?)', ('session-schema', 1))
    job = tmp_path / 'job'
    assert install_go_credential(job, source)
    with sqlite3.connect(job / 'opencode' / 'opencode.db') as db:
        # Every claimed migration has its matching schema, but no host data.
        assert db.execute('SELECT id FROM migration').fetchall() == [('session-schema',)]
        assert db.execute('SELECT count(*) FROM session').fetchone() == (0,)


@pytest.mark.parametrize('target', ['opencode-glm-5.3-flash-free',
                                   'opencode/glm-5.3-flash-free', 'disabled-combo',
                                   'aliases/disabled-combo'])
@pytest.mark.parametrize('client', [None, 'opencode'])
def test_runner_refuses_disabled_resolution(tmp_path, monkeypatch, target, client):
    from plangraph.planner.routing import SpawnRefused
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        {'id': 'opencode/glm-5.3-flash-free',
         'combos': ['disabled-combo', 'aliases/disabled-combo']}])
    monkeypatch.setattr(resolver, 'catalog_row_admitted', lambda row: True)
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    with pytest.raises(SpawnRefused, match='leg disabled'):
        asyncio.run(runner.spawn('test', model_or_combo=target, client=client, dry_run=True))


def test_disabled_skips_have_evidence():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import plan_spawn
    writer = next(l for l in DEFAULT_LEGS if 'implement' in l.roles)
    disabled = replace(writer, id='disabled-test-writer', disabled='synthetic disable')
    plan = plan_spawn({'estimate_tokens': 1000}, role='implement', legs=(writer, disabled))
    assert plan.skipped
    assert all('evidence' in entry for entry in plan.skipped)


def test_integration_is_opt_in():
    config = pytest.Config.fromdictargs({}, [])
    assert 'not integration' in config.getini('addopts')


def test_resolution_preserves_harness(monkeypatch):
    from plangraph.spawner.target import resolve_target
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        {'id': 'omniroute/cx-gpt-6.1-sol', 'combos': ['gpt-test']}])
    monkeypatch.setattr(resolver, 'catalog_row_admitted', lambda row: True)
    target, leg = resolve_target('gpt-test')
    assert target == leg.model == 'omniroute/cx-gpt-6.1-sol'
    assert leg.harness == 'opencode'


@pytest.mark.parametrize('target', ['disabled-combo', 'aliases/disabled-combo'])
def test_service_refuses_disabled_combo(tmp_path, monkeypatch, target):
    from plangraph.spawner.service import SpawnService
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        {'id': 'opencode/glm-5.3-flash-free', 'combos': [target]}])
    monkeypatch.setattr(resolver, 'catalog_row_admitted', lambda row: True)
    service = SpawnService(state=tmp_path)
    from plangraph.planner.routing import SpawnRefused
    with pytest.raises(SpawnRefused, match='leg disabled'):
        asyncio.run(service.spawn(card={'estimate_tokens': 1000}, prompt='test',
            model=target, override_reason='regression test', dry_run=True))
    assert not (tmp_path / 'spawn-overrides.jsonl').exists()


def test_disabled_and_fallback_routes():
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import plan_spawn
    card = {'estimate_tokens': 1000, 'risk_tags': []}
    plans = [plan_spawn(card, role=role, writer_families=('claude',))
             for role in ('review', 'implement')]
    for leg in DEFAULT_LEGS:
        if leg.model.startswith('opencode/') and leg.model.endswith('-free'):
            assert leg.disabled == 'D-4196 opencode zen never worked, use opencode go; never remove (D-3828)'
            for plan in plans:
                assert leg.id not in [l.id for l in plan.legs]
                if 'implement' in leg.roles or plan is plans[0]:
                    assert any(s['leg'] == leg.id and 'D-4196' in s['reason'] for s in plan.skipped)
                else:
                    assert not any(s['leg'] == leg.id for s in plan.skipped)
    assert 'omniroute-gemini-3.8-flash' not in [l.id for l in plans[0].legs]
    assert 'omniroute-gemini-3.8-flash' not in [l.id for l in plans[1].legs]


def test_source_path_uri_characters(tmp_path):
    source = tmp_path / 'source?#.db'
    source_store(source)
    assert install_go_credential(tmp_path / 'job', source)


def test_failed_write_leaves_no_database(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    source_store(source)
    connect = sqlite3.connect

    class BrokenConnection(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError('injected mid-write failure')

    def failing_connect(path, **kwargs):
        if not kwargs.get('uri'):
            kwargs['factory'] = BrokenConnection
        return connect(path, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', failing_connect)
    job = tmp_path / 'job'
    assert not install_go_credential(job, source)
    assert list((job / 'opencode').iterdir()) == []


@pytest.mark.integration
def test_real_models_probe(tmp_path):
    """Real CLI discovery with only selected auth, no host config or registry rows."""
    import os
    import shutil
    import subprocess
    from plangraph.spawner.opencode_isolation import close_config_layers, go_source_db

    executable = shutil.which('opencode')
    source = go_source_db()
    if executable is None or source is None or not source.is_file():
        if os.environ.get('PLANGRAPH_REQUIRE_GO_INTEGRATION') == '1':
            pytest.fail('required OpenCode integration prerequisites unavailable')
        pytest.skip('real OpenCode binary and credential store required')
    job = tmp_path / 'isolated-job'
    job.mkdir()
    config = None
    try:
        assert install_go_credential(job, source)
        env = {'PATH': os.environ['PATH'], 'HOME': str(job),
               'XDG_DATA_HOME': str(job), 'XDG_CONFIG_HOME': str(job / 'config')}
        config = close_config_layers(env)
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(
            {'permission': {'*': 'deny'}, 'mcp': {}, 'plugin': []})
        result = subprocess.run([executable, 'models', '--standalone'], cwd=job,
                                env=env, capture_output=True, text=True, timeout=90)
        # Never include raw command output in assertion failures: it may contain secrets.
        assert result.returncode == 0
        models = set(result.stdout.splitlines())
        assert {'opencode-go/glm-5.2', 'opencode-go/deepseek-v4.1-flash',
                'opencode-go/qwen3.8-max'} <= models
        with sqlite3.connect(job / 'opencode' / 'opencode.db') as db:
            values = db.execute('SELECT value FROM credential').fetchall()
        assert all(value not in result.stdout + result.stderr for (value,) in values if value)
    finally:
        try:
            if config:
                shutil.rmtree(config)
        finally:
            shutil.rmtree(job)
    assert not job.exists()
