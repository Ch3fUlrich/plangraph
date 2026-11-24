"""The review launch path applies, proves and records the read-only OpenCode seat policy."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner import opencode_isolation, resolver
from plangraph.spawner.opencode_isolation import (
    _DENIED_TOOLS, GO_AUTH_DB_ENV, apply_review_permissions, permission_denied_count,
    review_policy_refusal,
)
from plangraph.spawner.runner import SpawnerRunner, _opencode_tools_denied
from fake_pid import FAKE_PID
from test_opencode_isolation_go import source_store

DENIED_CALL = json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'state': {
    'status': 'error', 'error': 'Permission denied: external_directory'}}})
PLAIN_ERROR = json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'state': {
    'status': 'error', 'error': 'cargo: command not found'}}})


def config(repository_tools):
    env = {'OPENCODE_CONFIG_CONTENT': '{}'}
    apply_review_permissions(env, _DENIED_TOOLS, repository_tools=repository_tools)
    return json.loads(env['OPENCODE_CONFIG_CONTENT'])


@pytest.mark.parametrize('repository_tools', [False, True])
def test_applied_policy_passes_its_own_refusal_check(repository_tools):
    cfg = config(repository_tools)
    assert cfg['share'] == 'disabled' and cfg['mcp'] == {} and cfg['plugin'] == []
    assert cfg['permission']['external_directory'] == 'deny' and cfg['permission']['edit'] == 'deny'
    assert (cfg['permission']['read'] == 'allow') is repository_tools
    assert review_policy_refusal(json.dumps(cfg), _DENIED_TOOLS, repository_tools=repository_tools) is None


@pytest.mark.parametrize('edit', [
    lambda c: c['permission'].update(edit='allow'),
    lambda c: c['permission'].update(external_directory='allow'),
    lambda c: c['permission'].update(new_tool='allow'),
    lambda c: c['permission']['bash'].update({'rm *': 'allow'}),
    lambda c: c['permission'].update(bash='allow'),
    lambda c: c['permission']['bash'].update({'*': 'allow'}),
    lambda c: c.update(share='auto'),
    lambda c: c.pop('share'),
])
def test_any_extra_allow_refuses_the_launch(edit):
    cfg = config(True)
    edit(cfg)
    assert review_policy_refusal(json.dumps(cfg), _DENIED_TOOLS, repository_tools=True)


@pytest.mark.parametrize('edit,reason', [
    (lambda c: c['permission'].pop('*'), 'default deny is missing'),
    (lambda c: c.update(mcp={'x': {}}), 'adds a tool source'),
    (lambda c: c.update(plugin=['p']), 'adds a tool source'),
    (lambda c: c.pop('permission'), 'no permission object'),
])
def test_a_missing_default_deny_or_extra_tool_source_refuses_the_launch(edit, reason):
    cfg = config(True)
    edit(cfg)
    assert reason in review_policy_refusal(json.dumps(cfg), _DENIED_TOOLS, repository_tools=True)
    assert not _opencode_tools_denied(json.dumps(cfg), {})
    assert 'not JSON' in review_policy_refusal('not json', _DENIED_TOOLS, repository_tools=True)
    env = {'OPENCODE_CONFIG_CONTENT': 'not json'}
    assert 'not JSON' in apply_review_permissions(env, _DENIED_TOOLS, repository_tools=True)


def run_review(tmp_path, monkeypatch, stdout, tamper=None, leg=None):
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))

    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    if tamper:
        real = opencode_isolation.apply_review_permissions

        def apply(env, denied, *, repository_tools):
            assert real(env, denied, repository_tools=repository_tools) is None
            cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            tamper(cfg)
            env['OPENCODE_CONFIG_CONTENT'] = json.dumps(cfg)
            return review_policy_refusal(env['OPENCODE_CONFIG_CONTENT'], denied, repository_tools=repository_tools)
        monkeypatch.setattr(opencode_isolation, 'apply_review_permissions', apply)
    seen = []

    async def factory(*args, **kwargs):
        seen.append(json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT']))

        async def communicate(input=None):
            out = stdout(seen[-1]) if callable(stdout) else stdout
            return out.encode(), b''
        return SimpleNamespace(pid=FAKE_PID, returncode=0, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('review', model_or_combo=leg.model if leg else 'opencode-go-glm-5.3-flash',
                                 role='review', client='opencode', wait_seconds=3,
                                 **({'chosen': leg} if leg else {}))
        if job.job_id in runner._tasks:
            await runner._tasks[job.job_id]
        return job
    return asyncio.run(run()), seen


@pytest.mark.usefixtures('fake_ownership')
def test_launched_review_has_the_policy_and_records_the_denial(tmp_path, monkeypatch):
    job, seen = run_review(tmp_path, monkeypatch, DENIED_CALL + '\nVERDICT: ACCEPT\n')
    assert len(seen) == 1
    assert seen[0]['share'] == 'disabled'
    assert seen[0]['permission']['external_directory'] == 'deny' and seen[0]['permission']['edit'] == 'deny'
    assert {'event': 'permission_denied', 'count': 1} in job.events
    assert not any(e.get('event') == 'render_refused' for e in job.events)


@pytest.mark.usefixtures('fake_ownership')
def test_launch_with_an_extra_allow_is_refused_before_the_process_starts(tmp_path, monkeypatch):
    job, seen = run_review(tmp_path, monkeypatch, '',
                           tamper=lambda cfg: cfg['permission'].update(edit='allow'))
    assert seen == []
    refused = [e for e in job.events if e.get('event') == 'render_refused']
    assert refused and 'review_policy: edit is allowed' in refused[0]['reason']


def probe(permission, tool, target):
    """What OpenCode does with one tool call under `permission`: allowed, or the refusal event."""
    rule = permission.get(tool, permission['*'])
    if isinstance(rule, dict):
        rule = next((effect for pattern, effect in rule.items() if pattern != '*'
                     and target.startswith(pattern.rstrip('*'))), rule['*'])
    if rule == 'allow':
        return json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'state': {'status': 'completed'}}})
    return json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'state': {
        'status': 'error', 'error': f'Permission denied: {tool}'}}})


@pytest.mark.usefixtures('fake_ownership')
@pytest.mark.parametrize('leg_id', ['opencode-go-deepseek-v4-flash', 'opencode-go-glm-5.3-flash'])
def test_selected_tools_leg_runs_with_the_checkout_commands_and_its_denials_are_recorded(
        tmp_path, monkeypatch, leg_id):
    from plangraph.planner.legs import DEFAULT_LEGS
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)

    def stream(config):
        """The calls a seat makes, answered from the config the launch actually handed the child."""
        permission = config['permission']
        calls = [('read', 'src/a.rs'), ('bash', 'git diff HEAD'), ('bash', 'cargo test -p x'),
                 ('edit', 'src/a.rs'), ('bash', 'rm -rf target'), ('external_directory', '/outside/outside.txt'),
                 ('webfetch', 'https://example.test')]
        return '\n'.join(probe(permission, tool, target) for tool, target in calls) + '\nVERDICT: ACCEPT\n'

    job, seen = run_review(tmp_path, monkeypatch, stream, leg=leg)
    assert len(seen) == 1
    permission = seen[0]['permission']
    assert [p for p, e in permission['bash'].items() if e == 'allow'] == list(
        opencode_isolation._CHECKOUT_COMMANDS)
    assert permission['bash']['*'] == 'deny' and permission['edit'] == 'deny'
    assert permission['read'] == permission['glob'] == permission['grep'] == permission['list'] == 'allow'
    assert seen[0]['share'] == 'disabled' and seen[0]['mcp'] == {} and seen[0]['plugin'] == []
    # edit, `rm -rf`, external_directory and webfetch are refused; read and the checkout commands are not.
    assert {'event': 'permission_denied', 'count': 4} in job.events
