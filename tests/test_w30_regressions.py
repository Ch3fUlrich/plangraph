import asyncio
from dataclasses import replace

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner import resolver
from plangraph.spawner.runner import spawn_service

import json
import os
import pytest


@pytest.mark.parametrize('explicit', [False, True])
def test_real_binary_unavailable_and_secret(tmp_path, monkeypatch, caplog, explicit):
    from plangraph.spawner.runner import SpawnerRunner
    first = replace(DEFAULT_LEGS[0], harness='opencode', model='omniroute/cx-gpt-6.1-sol')
    second = replace(first, id='second', model='opencode-go/qwen3.8-max')
    binary = tmp_path / 'opencode'
    binary.write_text('#!/bin/sh\ncase "$*" in *cx-gpt*) echo "Error: Model unavailable: x $MY_GATEWAY_TOKEN" >&2; exit 1;; esac\necho success\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    monkeypatch.setenv('MY_GATEWAY_TOKEN', 'opaque-test-SECRET123')
    (tmp_path / 'opencode.jsonc').write_text(json.dumps({'providers': {'omniroute': {'env': ['MY_GATEWAY_TOKEN']}}}))
    service = spawn_service(state=tmp_path / 'state', runner=SpawnerRunner(default_cwd=str(tmp_path)))
    monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan((first, second), (), 'test', (), ()))
    async def continuation(*a):
        pass
    monkeypatch.setattr(service, '_continue_writer', continuation)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x',
                                       wait_seconds=None, no_fallthrough=explicit))
    assert result['status'] == ('failed' if explicit else 'completed')
    assert len(result['attempts']) == 2
    assert len([a for a in result['attempts'] if a['status'] != 'skipped']) == (1 if explicit else 2)
    if explicit:
        assert result['attempts'][1]['skip_reason'] == 'run_terminated_before_leg'
    assert 'opaque-test-SECRET123' not in service._path(result['run_id']).read_text() + caplog.text
    assert service.runner._child_envs == {}


def test_dry_run_never_probes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(resolver, 'served_models', lambda config: calls.append(config) or frozenset())
    service = spawn_service(state=tmp_path)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x', dry_run=True))
    assert result['status'] == 'dry_run'
    assert calls == []


def test_real_launch_plan_skips_unserved(monkeypatch):
    # Pin a present (empty) root config so the reason does not depend on the host's AUTOOS_ROOT.
    from plangraph.routing_support import opencode_overlay
    monkeypatch.setattr(opencode_overlay, '_load_root_providers', lambda: ({}, None))
    first = replace(DEFAULT_LEGS[0], harness='opencode', model='test/absent')
    second = replace(first, id='second', model='test/present')
    plan = SpawnPlan((first, second), (), 'test', (), ())
    filtered = resolver.plan_for_launch(plan, probe=lambda env: {'test/present'})
    assert filtered.legs == (second,)
    assert filtered.skipped[0]['skip_reason'] == 'not_served_on_host: test/absent'


def test_explicit_leg_unavailable_one_attempt(tmp_path, monkeypatch):
    from plangraph.spawner.runner import SpawnerRunner
    leg = next(l for l in DEFAULT_LEGS if l.harness == 'opencode' and l.family != 'deepseek')  # D-2744
    binary = tmp_path / 'opencode'
    # W33B host admission probes are not child attempts: serve the requested id.
    binary.write_text('#!/bin/sh\nif [ "$1" = models ]; then echo "' + leg.model + '"; exit 0; fi\necho launched >> "' + str(tmp_path / 'launches') + '"\necho "Model unavailable: fixture" >&2\nexit 1\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    (tmp_path / 'opencode.jsonc').write_text('{"providers":{}}')
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)
    service = spawn_service(state=tmp_path, runner=SpawnerRunner(default_cwd=str(tmp_path)))
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x',
        leg=leg.id, override_reason='W30 unavailable model regression', wait_seconds=None))
    assert result['status'] == 'failed'
    assert len(result['attempts']) == 1
    assert 'Model unavailable: fixture' in result['stderr_tail']
    assert (tmp_path / 'launches').read_text().splitlines() == ['launched']


def test_agy_does_not_receive_overlay(tmp_path, monkeypatch):
    from plangraph.spawner.runner import SpawnerRunner
    binary = tmp_path / 'agy'
    binary.write_text('#!/bin/sh\nif [ -n "$OPENCODE_CONFIG_CONTENT" ]; then echo overlay-leaked; exit 1; fi\necho clean\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.delenv('OPENCODE_CONFIG_CONTENT', raising=False)
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path))
    (tmp_path / 'opencode.jsonc').write_text('{"providers":{"fixture":{}}}')
    async def run():
        runner = SpawnerRunner(default_cwd=str(tmp_path))
        job = await runner.spawn('x', 'gemini-3.8-flash', client='agy')
        await runner._tasks[job.job_id]
        return job
    job = asyncio.run(run())
    assert job.exit_code == 0
    assert 'clean' in job.stdout
