"""Role config isolation without launching a model or reading real user config."""
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from plangraph.spawner import runner as module


@pytest.fixture
def seat(tmp_path, monkeypatch):
    from plangraph.spawner import opencode_overlay, resolver
    from plangraph.routing_support import opencode_render, host_keys
    monkeypatch.setattr(opencode_overlay, 'overlay_env', lambda env: env)
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda *a: None)
    monkeypatch.setattr(host_keys, 'add_host_keys', lambda *a, **kw: None)
    async def available(*a, **kw):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', available)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('PARENT_SECRET', 'sentinel-secret-not-for-config')
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{"instructions":["/x/leaf-contract.md"]}')
    root = tmp_path / 'config' / 'opencode'
    root.mkdir(parents=True)
    config = {'instructions': ['/x/leaf-contract.md'],
              'agent': {'orchestrator': {'mode': 'primary'}},
              'permission': {'bash': 'ask'}}
    (root / 'opencode.json').write_text(json.dumps(config))
    return root, config


def capture(monkeypatch, role, *, dry_run=True, fail=False):
    runner = module.SpawnerRunner()
    observed = {}
    class Envs(dict):
        def __setitem__(self, key, env):
            observed['env'] = env.copy()
            directory = Path(env['XDG_CONFIG_HOME'])
            observed['directory'] = directory
            if role == 'review':
                observed['denied'] = module._opencode_tools_denied(env['OPENCODE_CONFIG_CONTENT'], env)
            if role != 'review':
                observed['config'] = json.loads((directory / 'opencode/opencode.json').read_text())
                observed['text'] = (directory / 'role-instructions.md').read_text()
                observed['mode'] = directory.stat().st_mode & 0o777
                observed['written'] = '\n'.join(p.read_text() for p in directory.rglob('*') if p.is_file())
            super().__setitem__(key, env)
    runner._child_envs = Envs()
    async def factory(*a, **kw):
        observed['launched'] = True
        if fail:
            raise OSError('launch failed')
        class Process:
            pid = 99999999
            returncode = 0
            async def communicate(self, *a, **kw):
                return b'', b''
        return Process()
    runner.process_factory = factory
    async def reap(*a):
        observed['reaped'] = True
        assert observed['directory'].exists()
    monkeypatch.setattr(runner, '_reap_process', reap)
    async def run():
        try:
            job = await runner.spawn('brief', model_or_combo='mimo/mimo-v2.6-pro',
                                     role=role, dry_run=dry_run)
            if not dry_run:
                await runner._tasks[job.job_id]
            return job
        except OSError:
            if not fail:
                raise
    return asyncio.run(run()), observed


@pytest.mark.parametrize('role', ['implement', 'orchestrate'])
def test_role_config(seat, monkeypatch, role):
    job, seen = capture(monkeypatch, role)
    config = seen['config']
    path = str(seen['directory'] / 'role-instructions.md')
    assert config == {**seat[1], 'instructions': [path]}
    assert seen['text'] == module.ROLE_INSTRUCTIONS[role]
    assert seen['mode'] == 0o700
    assert '/x/leaf-contract.md' not in json.dumps(config)
    assert 'sentinel-secret-not-for-config' not in json.dumps(config)
    assert 'sentinel-secret-not-for-config' not in seen['written']
    overlay = json.loads(seen['env']['OPENCODE_CONFIG_CONTENT'])
    assert overlay['instructions'] == [path]
    if role == 'orchestrate':
        assert set(overlay['mcp']) == {'plangraph', 'plangraph-spawner'}
    assert seen['env']['XDG_CONFIG_HOME'] == str(seen['directory'])
    assert not seen['directory'].exists()


def test_review_unchanged(seat, monkeypatch):
    job, seen = capture(monkeypatch, 'review')
    env = seen['env']
    assert Path(env['XDG_CONFIG_HOME']) == Path(env['HOME']) / 'config'
    assert 'pg-ocdata-' in env['HOME']
    assert 'pg-occfg-' not in json.dumps(env)
    assert seen['denied']
    assert not any(e['event'] == 'role_instructions' for e in job.events)


@pytest.mark.parametrize('source', ['jsonc', 'invalid', 'missing', 'home'])
def test_global_config_formats(seat, monkeypatch, source):
    root, config = seat
    (root / 'opencode.json').unlink()
    if source == 'jsonc':
        (root / 'opencode.jsonc').write_text('// comment\n' + json.dumps(config))
    elif source == 'invalid':
        (root / 'opencode.json').write_text('broken')
    elif source == 'home':
        monkeypatch.delenv('XDG_CONFIG_HOME')
        home = Path.home() / '.config/opencode'
        home.mkdir(parents=True)
        (home / 'opencode.json').write_text(json.dumps(config))
    job, seen = capture(monkeypatch, 'implement')
    if source in ('invalid', 'missing'):
        assert set(seen['config']) == {'instructions'}
        assert {'event': 'global_config_unreadable'} in job.events
    else:
        assert seen['config']['agent'] == config['agent']
        assert {'event': 'global_config_unreadable'} not in job.events


@pytest.mark.parametrize('fail', [False, True])
def test_config_cleaned_after_reap_or_spawn_failure(seat, monkeypatch, fail):
    _, seen = capture(monkeypatch, 'orchestrate', dry_run=False, fail=fail)
    assert seen['launched']
    assert seen.get('reaped', False) is not fail
    assert not seen['directory'].exists()


def test_config_files_private_and_provider_key_isolated(seat, monkeypatch):
    """F1: both per-job config files are 0600, and an inline provider key from
    the global config lands only in that owner-only file -- never in
    OPENCODE_CONFIG_CONTENT or the job events."""
    root, config = seat
    secret = 'sentinel-inline-provider-key-9241'
    (root / 'opencode.json').write_text(json.dumps(
        {**config, 'providers': {'mimo': {'options': {'apiKey': secret}}}}))
    real_rmtree = shutil.rmtree
    monkeypatch.setattr(shutil, 'rmtree', lambda path, **kw: None)
    job, seen = capture(monkeypatch, 'orchestrate')
    directory = seen['directory']
    try:
        assert (directory / 'role-instructions.md').stat().st_mode & 0o777 == 0o600
        written = directory / 'opencode' / 'opencode.json'
        assert written.stat().st_mode & 0o777 == 0o600
        assert json.loads(written.read_text())['providers']['mimo']['options']['apiKey'] == secret
        assert secret not in seen['env']['OPENCODE_CONFIG_CONTENT']
        assert secret not in json.dumps(job.events)
    finally:
        real_rmtree(directory, ignore_errors=True)


def test_global_dir_entries_symlinked(seat, monkeypatch):
    """F2: every other entry of the global opencode dir (plugins, agent md)
    stays reachable through the child's XDG_CONFIG_HOME as a symlink to the
    original -- never copied."""
    root, config = seat
    (root / 'agent').mkdir()
    (root / 'agent' / 'x.md').write_text('agent markdown body')
    (root / 'plugin').mkdir()
    (root / 'plugin' / 'y.js').write_text('plugin source body')
    real_rmtree = shutil.rmtree
    removed = []
    monkeypatch.setattr(shutil, 'rmtree', lambda path, **kw: removed.append(str(path)))
    job, seen = capture(monkeypatch, 'implement')
    opencode = seen['directory'] / 'opencode'
    try:
        assert (opencode / 'agent').is_symlink()
        assert (opencode / 'plugin').is_symlink()
        assert (opencode / 'agent').readlink() == Path(root / 'agent')
        assert (opencode / 'agent' / 'x.md').read_text() == 'agent markdown body'
        assert (opencode / 'plugin' / 'y.js').read_text() == 'plugin source body'
        assert (opencode / 'opencode.json').is_file()
        assert not (opencode / 'opencode.json').is_symlink()
        assert any('pg-occfg-' in name for name in removed)  # cleanup still runs
    finally:
        real_rmtree(seen['directory'], ignore_errors=True)


def test_occfg_removed_on_render_refusal(seat, monkeypatch):
    """An early return before launch (render_refused) still pops and removes
    the per-job config dir."""
    from plangraph.routing_support import opencode_render
    monkeypatch.setattr(opencode_render, 'render_gateway_providers',
                        lambda *a: 'refused: no providers')
    created = []
    original = tempfile.mkdtemp
    def hooked(*args, **kwargs):
        name = original(*args, **kwargs)
        if kwargs.get('prefix') == 'pg-occfg-':
            created.append(name)
        return name
    monkeypatch.setattr(tempfile, 'mkdtemp', hooked)
    runner = module.SpawnerRunner()
    async def factory(*a, **kw):
        raise AssertionError('a refused run must not launch')
    runner.process_factory = factory
    job = asyncio.run(runner.spawn('brief', model_or_combo='mimo/mimo-v2.6-pro',
                                   role='orchestrate', dry_run=False))
    assert job.status == 'failed'
    assert {'event': 'render_refused', 'reason': 'refused: no providers'} in job.events
    assert created and not any(Path(name).exists() for name in created)
    assert job.job_id not in runner._occfg_dirs
