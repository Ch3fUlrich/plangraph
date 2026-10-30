"""D-3236: writers run without the global opencode config; a writer that changes nothing fails.

Live: run-fcf3bb7d (sol writer) loaded the operator's global opencode config, whose
`instructions` (an AutoOS leaf contract) forbid commits, and exited 0 after 22 s with
no change -- recorded as completed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.spawner import resolver
from plangraph.spawner.runner import SpawnerRunner


def _git_repo(path):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    for key, value in (('user.email', 't@example.invalid'), ('user.name', 't'), ('commit.gpgsign', 'false')):
        subprocess.run(['git', '-C', str(path), 'config', key, value], check=True)
    (path / 'a.txt').write_text('a\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-qm', 'init'], check=True)
    return path


def _run_writer(tmp_path, monkeypatch, work, *, act=lambda work: None, role='implement', home=None):
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    if home is not None:
        monkeypatch.setenv('HOME', str(home))
        monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    seen = {}
    async def factory(*args, **kwargs):
        seen['env'] = dict(kwargs['env'])
        seen['env']['_config_text'] = Path(kwargs['env']['OPENCODE_CONFIG']).read_text()
        act(work)
        async def communicate():
            return b'done', b''
        return SimpleNamespace(pid=123, returncode=0, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(work), process_factory=factory)
    async def run():
        job = await runner.spawn('prompt', model_or_combo='omniroute/cx-gpt-6.1-sol', role=role,
                                 client='opencode', cwd=str(work), wait_seconds=3)
        await runner._tasks[job.job_id]
        return job
    return asyncio.run(run()), seen['env']


def test_writer_gets_a_private_config_dir_not_the_global_one(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    global_dir = home / '.config' / 'opencode'
    global_dir.mkdir(parents=True)
    (global_dir / 'opencode.json').write_text(json.dumps({'instructions': ['leaf-contract.md']}))
    work = tmp_path / 'work'
    work.mkdir()
    job, env = _run_writer(tmp_path, monkeypatch, work, home=home)
    config_dir = env['OPENCODE_CONFIG_DIR']
    assert Path(config_dir) != global_dir and not Path(config_dir).is_relative_to(home)
    assert json.loads(env['_config_text']) == {}
    for flag in ('OPENCODE_DISABLE_PROJECT_CONFIG', 'OPENCODE_DISABLE_CLAUDE_CODE',
                 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS'):
        assert env[flag] == '1'
    assert env['HOME'] == str(home)   # a writer keeps HOME (git identity, auth data)
    assert 'leaf-contract.md' not in env.get('OPENCODE_CONFIG_CONTENT', '')
    assert not Path(config_dir).exists()   # removed after the run
    assert job.status == 'completed'


@pytest.mark.writer_change_check
def test_writer_that_changes_nothing_fails(tmp_path, monkeypatch):
    work = _git_repo(tmp_path / 'work')
    job, _ = _run_writer(tmp_path, monkeypatch, work)
    assert job.status == 'failed' and job.exit_code == 4
    assert {'event': 'writer_no_changes'} in job.events
    assert job.stderr.startswith('writer_no_changes:')


@pytest.mark.writer_change_check
@pytest.mark.parametrize('act', [
    lambda w: (w / 'a.txt').write_text('b\n'),
    lambda w: subprocess.run(['git', '-C', str(w), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True),
])
def test_writer_with_a_change_or_commit_completes(tmp_path, monkeypatch, act):
    work = _git_repo(tmp_path / 'work')
    job, _ = _run_writer(tmp_path, monkeypatch, work, act=act)
    assert job.status == 'completed' and job.exit_code == 0


@pytest.mark.writer_change_check
def test_review_seat_is_not_held_to_a_change(tmp_path, monkeypatch):
    work = _git_repo(tmp_path / 'work')
    job, _ = _run_writer(tmp_path, monkeypatch, work, role='review')
    assert job.status == 'completed'
    assert not any(e.get('event') == 'writer_baseline' for e in job.events)
