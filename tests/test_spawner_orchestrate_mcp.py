"""Hermetic coverage of the opencode orchestrator seat."""
import asyncio
import json
import sys

import pytest

from plangraph.spawner.runner import SpawnerRunner


@pytest.fixture
def seat(monkeypatch):
    from plangraph.spawner import opencode_overlay
    from plangraph.routing_support import opencode_render, host_keys
    monkeypatch.setattr(opencode_overlay, 'overlay_env', lambda env: env)
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda *args: None)
    monkeypatch.setattr(host_keys, 'add_host_keys', lambda *args, **kwargs: None)
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{"mcp":{"original":{"enabled":false}}}')
    for name in ('FLEET_MSG_INVENTORY', 'FLEET_MSG_HOST', 'FLEET_MSG_SPOOL',
                 'FLEET_MSG_WAKE', 'FLEET_MSG_STATUS_FILE'):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def spawn(role='orchestrate', client=None):
    runner = SpawnerRunner()
    class CapturedEnvs(dict):
        def pop(self, key, default=None):
            return self.get(key, default)
    runner._child_envs = CapturedEnvs()
    job = asyncio.run(runner.spawn('do the brief', model_or_combo='mimo/mimo-v2.6-pro',
                                   role=role, dry_run=True, client=client))
    return job, runner._child_envs[job.job_id]


def test_orchestrate_mcp_config(seat):
    job, env = spawn()
    assert json.loads(env['OPENCODE_CONFIG_CONTENT'])['mcp'] == {
        name: {'type': 'local', 'command': [sys.executable, '-m', module], 'enabled': True}
        for name, module in [('plangraph', 'plangraph.keeper.mcp'),
                             ('plangraph-spawner', 'plangraph.spawner.mcp')]}
    assert {'event': 'orchestrate_mcp', 'servers': ['plangraph', 'plangraph-spawner']} in job.events


@pytest.mark.parametrize('role', ['orchestrate', 'implement', 'review'])
def test_orchestrator_agent_and_header_only_for_orchestrate(seat, role):
    job, env = spawn(role)
    if role == 'orchestrate':
        from plangraph.spawner.runner import ORCHESTRATE_HEADER
        assert job.task_prompt == ORCHESTRATE_HEADER + '\n\ndo the brief'
        index = job.command.index('--title')
        assert job.command[index + 2:index + 4] == ['--agent', 'orchestrator']
    else:
        assert '--agent' not in job.command
        assert json.loads(env['OPENCODE_CONFIG_CONTENT'])['mcp'] == (
            {} if role == 'review' else {'original': {'enabled': False}})


def test_orchestrate_bus_env(seat):
    names = ('FLEET_MSG_INVENTORY', 'FLEET_MSG_HOST', 'FLEET_MSG_SPOOL',
             'FLEET_MSG_WAKE', 'FLEET_MSG_STATUS_FILE', 'PG_DSN', 'PG_TOKEN_FILE', 'PYTHONPATH')
    for name in names:
        seat.setenv(name, 'private-parent-value-' + name)
    job, env = spawn()
    assert env['PLANGRAPH_ORCH_INBOX'] == job.job_id
    for name in names:
        assert env[name] == 'private-parent-value-' + name
        assert env[name] not in env['OPENCODE_CONFIG_CONTENT']
    assert {'event': 'orchestrate_no_bus'} not in job.events


def test_orchestrate_no_bus(seat):
    job, env = spawn()
    assert {'event': 'orchestrate_no_bus'} in job.events


def test_orchestrate_non_opencode(seat):
    job, env = spawn(client='claude')
    assert job.command[0] != 'opencode'
    assert 'OPENCODE_CONFIG_CONTENT' not in env
    assert not any(e['event'].startswith('orchestrate_') for e in job.events)


def test_orchestrate_non_opencode_refused_before_launch(seat):
    """F3: a non-opencode harness has no plangraph-spawner MCP tools, so an
    orchestrate run is refused before any process starts."""
    runner = SpawnerRunner()
    started = []
    async def factory(*args, **kwargs):
        started.append(args)
        raise AssertionError('a refused run must not launch')
    runner.process_factory = factory
    job = asyncio.run(runner.spawn('do the brief', model_or_combo='mimo/mimo-v2.6-pro',
                                   role='orchestrate', dry_run=False, client='claude'))
    assert job.status == 'failed'
    assert job.events == [{'event': 'orchestrate_needs_opencode'}]
    assert not started


def test_orchestrate_git_verb_deny_permissions(seat):
    """F4: 'never merge or push to main / never commit' is machine-enforced in
    OPENCODE_CONFIG_CONTENT for orchestrate, merged into any existing bash
    permission map; implement keeps its map untouched."""
    seat.setenv('OPENCODE_CONFIG_CONTENT', json.dumps(
        {'mcp': {'original': {'enabled': False}},
         'permission': {'bash': {'npm*': 'allow'}, 'edit': 'ask'}}))
    deny = {'git push*': 'deny', 'git merge*': 'deny', 'git commit*': 'deny',
            'git rebase*': 'deny', 'git reset*': 'deny'}
    job, env = spawn('orchestrate')
    permission = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
    assert permission['bash'] == {'npm*': 'allow', **deny}
    assert permission['edit'] == 'ask'
    job, env = spawn('implement')
    assert json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission'] == (
        {'bash': {'npm*': 'allow'}, 'edit': 'ask'})
    seat.setenv('OPENCODE_CONFIG_CONTENT', '{"mcp":{"original":{"enabled":false}}}')
    job, env = spawn('orchestrate')
    assert json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']['bash'] == deny


def test_orchestrate_bash_string_shorthand_kept(seat):
    """A string bash policy keeps applying to every other command."""
    seat.setenv('OPENCODE_CONFIG_CONTENT', json.dumps({'permission': {'bash': 'ask'}}))
    job, env = spawn('orchestrate')
    bash = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']['bash']
    assert bash['*'] == 'ask' and bash['git push*'] == 'deny'
