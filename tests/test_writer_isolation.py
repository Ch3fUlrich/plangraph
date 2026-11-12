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
from fake_pid import FAKE_PID
pytestmark = pytest.mark.usefixtures('fake_ownership')   # r3 V3: FAKE_PID exceeds the host PID range; no live process can match

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
        async def communicate(input=None):
            return b'done', b''
        return SimpleNamespace(pid=FAKE_PID, returncode=0, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(work), process_factory=factory)
    async def run():
        job = await runner.spawn('prompt', model_or_combo='omniroute/cx-gpt-6.1-sol-xhigh', role=role,
                                 client='opencode', cwd=str(work), wait_seconds=3,
                                 extra_env={'GIT_AUTHOR_EMAIL': 'run-test@writer.plangraph.invalid'})
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
    assert job.status == 'failed' and job.exit_code == 5   # not 4: that is exhausted legs (r2 seat)
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
def test_writer_in_a_repo_without_commits_is_still_checked(tmp_path, monkeypatch):
    # Seat finding (qwen run-5ce67bc0): an unborn HEAD made tree_state None and the gate a no-op.
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    job, _ = _run_writer(tmp_path, monkeypatch, work)
    assert job.status == 'failed' and {'event': 'writer_no_changes'} in job.events
    job, _ = _run_writer(tmp_path, monkeypatch, work, act=lambda w: (w / 'new.txt').write_text('x'))
    assert job.status == 'completed'


@pytest.mark.writer_change_check
def test_review_seat_is_not_held_to_a_change(tmp_path, monkeypatch):
    work = _git_repo(tmp_path / 'work')
    job, _ = _run_writer(tmp_path, monkeypatch, work, role='review')
    assert job.status == 'completed'
    assert not any(e.get('event') == 'writer_baseline' for e in job.events)


@pytest.mark.writer_change_check
def test_writers_sharing_a_cwd_are_both_left_unchecked(tmp_path):
    # r2 seats (qwen run-9ee7830a, mimo-pro run-3b4a1f22): one whole-tree hash cannot judge two
    # writers in one cwd -- B's edit would pass an inert A.
    from plangraph.spawner.opencode_isolation import UNCHECKED, fail_unchanged_writer, record_baseline
    work = _git_repo(tmp_path / 'work')
    def job(job_id, role='implement'):
        return SimpleNamespace(job_id=job_id, role=role, cwd=str(work), status='running', events=[],
                               exit_code=None, stderr='')
    a, b, seat = job('a'), job('b'), job('s', role='review')
    record_baseline(a, work, [seat])
    assert a.events[0]['event'] == 'writer_baseline'
    record_baseline(b, work, [a, b, seat])
    assert UNCHECKED in a.events and UNCHECKED in b.events and not seat.events
    record_baseline(seat, work, [a, b, seat])   # r3 seat (mimo-pro): a review seat marks nobody
    assert a.events.count(UNCHECKED) == 1 and not seat.events
    a.status = 'completed'
    fail_unchanged_writer(a, work)
    assert a.status == 'completed'


def test_unchanged_check_ignores_non_writer_roles(tmp_path):
    from plangraph.spawner.opencode_isolation import fail_unchanged_writer
    work = _git_repo(tmp_path / 'work')
    from plangraph.spawner.opencode_isolation import tree_state
    job = SimpleNamespace(role='review', status='completed', exit_code=0, stderr='',
                          events=[{'event': 'writer_baseline', 'state': tree_state(work)}])
    fail_unchanged_writer(job, work)
    assert job.status == 'completed' and job.exit_code == 0


@pytest.mark.parametrize('env', [{'HOME': '/srv/u', 'XDG_CONFIG_HOME': '/srv/u/.config'},
                                 {'USERPROFILE': r'D:\profiles\u', 'APPDATA': r'D:\profiles\u\AppData\Roaming'}])
def test_config_layers_close_independent_of_platform_home(env):
    # r2 seat (mimo-pro): the closed layers do not read HOME/XDG (POSIX) or USERPROFILE (Windows).
    # Env-shape only: a real Windows global-layer run stays an operator check (docstring).
    import shutil
    from plangraph.spawner.opencode_isolation import CLOSED_LAYERS, close_config_layers
    child = dict(env)
    oc_dir = close_config_layers(child)
    try:
        assert child['OPENCODE_CONFIG_DIR'] == oc_dir and Path(child['OPENCODE_CONFIG']).parent == Path(oc_dir)
        assert all(not oc_dir.startswith(v) for v in env.values())
        assert json.loads(Path(child['OPENCODE_CONFIG']).read_text()) == {}
        assert all(child[k] == v for k, v in CLOSED_LAYERS.items())
    finally:
        shutil.rmtree(oc_dir)


@pytest.mark.writer_change_check
@pytest.mark.parametrize('change', ['commit', 'other', 'none', 'fetch', 'pushed_deleted', 'deleted', 'committer', 'backdated'])
def test_writer_shared_local_refs(tmp_path, monkeypatch, change):
    work = _git_repo(tmp_path / 'work')
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(work), 'worktree', 'add', '-qb', 'writer', str(linked)], check=True)
    remote = _git_repo(tmp_path / 'remote')
    subprocess.run(['git', '-C', str(work), 'fetch', '-q', str(remote), 'HEAD:refs/remotes/test/main'], check=True)
    def act(_):
        if change in ('commit', 'other', 'pushed_deleted', 'deleted', 'committer', 'backdated'):
            email = ('run-other' if change == 'other' else 'run-test') + '@writer.plangraph.invalid'
            subprocess.run(['git', '-C', str(linked), '-c', 'user.email=' + email,
                            'commit', '-q', '--allow-empty', '-m', 'writer work',
                            *(['--author=Other <other@example.invalid>'] if change == 'committer' else [])],
                           env=({**__import__('os').environ, 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00Z'}
                                if change == 'backdated' else None), check=True)
            if change == 'deleted':
                subprocess.run(['git', '-C', str(linked), 'checkout', '-q', '--detach', 'HEAD~1'], check=True)
                subprocess.run(['git', '-C', str(work), 'branch', '-D', 'writer'], check=True)
            if change == 'pushed_deleted':
                subprocess.run(['git', '-C', str(linked), 'push', '-q', str(remote),
                                'HEAD:refs/heads/saved'], check=True)
                subprocess.run(['git', '-C', str(linked), 'checkout', '-q', '--detach'], check=True)
                subprocess.run(['git', '-C', str(work), 'branch', '-D', 'writer'], check=True)
                subprocess.run(['git', '-C', str(work), 'fetch', '-q', str(remote),
                                'refs/heads/saved:refs/remotes/test/saved'], check=True)
        elif change == 'fetch':
            subprocess.run(['git', '-C', str(remote), 'commit', '-q', '--allow-empty', '-m', 'remote work'], check=True)
            subprocess.run(['git', '-C', str(work), 'fetch', '-q', str(remote), 'HEAD:refs/remotes/test/main'], check=True)
    job, _ = _run_writer(tmp_path, monkeypatch, work, act=act)
    assert (job.status, job.exit_code) == (('completed', 0) if change in ('commit', 'pushed_deleted', 'deleted', 'committer', 'backdated') else ('failed', 5))


@pytest.mark.parametrize('email', [None, ''])
def test_missing_writer_identity_is_unchecked(tmp_path, email):
    from plangraph.spawner import opencode_isolation as isolation
    work = _git_repo(tmp_path / 'work')
    job = SimpleNamespace(role='implement', status='completed', exit_code=0, stderr='',
                          writer_email=email, events=[{'event': 'writer_baseline', 'state': isolation.tree_state(work)}])
    isolation.fail_unchanged_writer(job, work)
    assert (job.status, job.exit_code) == ('completed', 0)
    assert job.events[-1]['event'] == 'writer_change_unchecked'


@pytest.mark.writer_change_check
def test_earlier_attempt_commit_does_not_count(tmp_path, monkeypatch):
    work = _git_repo(tmp_path / 'work')
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(work), 'worktree', 'add', '-qb', 'writer', str(linked)], check=True)
    def earlier_attempt(_):
        subprocess.run(['git', '-C', str(linked), '-c',
                        'user.email=run-test@writer.plangraph.invalid', 'commit', '-q',
                        '--allow-empty', '-m', 'earlier attempt'], check=True)
    first, _ = _run_writer(tmp_path, monkeypatch, work, act=earlier_attempt)
    assert (first.status, first.exit_code) == ('completed', 0)
    later, _ = _run_writer(tmp_path, monkeypatch, work)
    assert (later.status, later.exit_code) == ('failed', 5)
    assert {'event': 'writer_no_changes'} in later.events


def test_tree_state_outside_worktree(tmp_path):
    from plangraph.spawner.opencode_isolation import tree_state
    assert tree_state(tmp_path) is None


def test_writer_commit_lookup_error_is_unchecked(tmp_path, monkeypatch):
    from plangraph.spawner import opencode_isolation as isolation
    work = _git_repo(tmp_path / 'work')
    job = SimpleNamespace(role='implement', status='completed', exit_code=0, stderr='',
                          writer_email='run-test@writer.plangraph.invalid', start_time=0,
                          events=[{'event': 'writer_baseline', 'state': isolation.tree_state(work)}])
    real_run = subprocess.run
    def broken_log(args, **kwargs):
        if 'log' in args:
            raise subprocess.CalledProcessError(128, args)
        return real_run(args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', broken_log)
    isolation.fail_unchanged_writer(job, work)
    assert (job.status, job.exit_code) == ('completed', 0)
    assert job.events[-1] == {'event': 'writer_change_unchecked', 'reason': 'writer commit lookup failed'}
