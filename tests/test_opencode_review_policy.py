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
])
def test_any_extra_allow_refuses_the_launch(edit):
    cfg = config(True)
    edit(cfg)
    assert review_policy_refusal(json.dumps(cfg), _DENIED_TOOLS, repository_tools=True)


def test_a_missing_deny_or_extra_surface_is_left_to_the_witness_proof():
    for edit in (lambda c: c['permission'].pop('webfetch'), lambda c: c.update(mcp={'x': {}}),
                 lambda c: c.update(plugin=['p']), lambda c: c.pop('permission')):
        cfg = config(True)
        edit(cfg)
        assert review_policy_refusal(json.dumps(cfg), _DENIED_TOOLS, repository_tools=True) is None
        assert not _opencode_tools_denied(json.dumps(cfg), {})
    assert review_policy_refusal('not json', _DENIED_TOOLS, repository_tools=True) is None


def test_denied_calls_are_counted_and_plain_tool_errors_are_not():
    stream = '\n'.join([DENIED_CALL, PLAIN_ERROR, 'stderr text', DENIED_CALL])
    assert permission_denied_count(stream) == 2
    assert permission_denied_count(PLAIN_ERROR) == permission_denied_count('') == 0


def run_review(tmp_path, monkeypatch, stdout, tamper=None):
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))

    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    if tamper:
        real = opencode_isolation.apply_review_permissions

        def apply(env, denied, *, repository_tools):
            real(env, denied, repository_tools=repository_tools)
            cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            tamper(cfg)
            env['OPENCODE_CONFIG_CONTENT'] = json.dumps(cfg)
        monkeypatch.setattr(opencode_isolation, 'apply_review_permissions', apply)
    seen = []

    async def factory(*args, **kwargs):
        seen.append(json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT']))

        async def communicate(input=None):
            return stdout.encode(), b''
        return SimpleNamespace(pid=FAKE_PID, returncode=0, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('review', model_or_combo='opencode-go-glm-5.3-flash',
                                 role='review', client='opencode', wait_seconds=3)
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
