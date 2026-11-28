"""A Codex writer probe runs where its commit can land; a sandbox denial is a probe defect, never a leg failure."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.routing_support.clients import build_command
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner import leg_probe
from plangraph.spawner.runner import SpawnerRunner

# What codex's workspace-write sandbox does to the probe repo: the work tree is writable, `.git` stays
# read-only unless `--add-dir <repo>/.git` names it. The emulator enforces that on the real directory,
# so the writer's own `git add` / `git commit` succeed or fail for real.
CODEX = r'''
import os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
cwd = Path.cwd().resolve()
granted = False
for i, a in enumerate(args):
    if a == '--sandbox' and args[i + 1] == 'workspace-write':
        granted = any(args[j] == '--add-dir' and Path(args[j + 1]).resolve() == cwd / '.git' for j in range(len(args) - 1))
if not granted:
    for root, dirs, files in os.walk('.git'):
        for name in dirs + files:
            os.chmod(os.path.join(root, name), 0o444 if name in files else 0o555)
    os.chmod('.git', 0o555)
(cwd / 'ok.txt').write_text('ok')
env = dict(os.environ)
for step in (['git', 'add', 'ok.txt'], ['git', 'commit', '-qm', 'probe']):
    done = subprocess.run(step, capture_output=True, text=True, env=env)
    sys.stderr.write(done.stderr.replace('Permission denied', 'Read-only file system'))  # the sandbox answers EROFS
    if done.returncode:
        sys.exit(0)
'''


def _leg():
    return SimpleNamespace(id='cx', model='gpt-6.2-sol-medium', harness='codex', roles=('implement',))


def test_codex_writer_command_is_workspace_write_with_the_git_dir(monkeypatch):
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client', lambda *a, **k: ('codex', None))
    cmd = build_command('p', 'gpt-6.2-sol-medium', role='implement', client='codex', writable_dirs=['/r/.git'])
    assert cmd[cmd.index('--sandbox') + 1] == 'workspace-write'
    assert cmd[cmd.index('--add-dir') + 1] == '/r/.git'
    assert cmd[-1] == '-'
    plain = build_command('p', 'gpt-6.2-sol-medium', role='implement', client='codex')
    assert '--sandbox' not in plain and '--add-dir' not in plain


def _runner(tmp_path, monkeypatch, *, drop_grant=False):
    """The production launch path (leg_probe -> SpawnerRunner.spawn -> build_command) with a sandboxed codex."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **k: '/bin/codex')
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client', lambda *a, **k: ('codex', None))
    seen = []

    async def factory(exe, *args, **kwargs):
        argv = list(args)
        if drop_grant:  # the old command: no sandbox flags at all
            argv = [a for a in argv if a not in ('--sandbox', 'workspace-write', '--add-dir')
                    and not a.endswith('/.git')]
        seen.append(argv)
        return await asyncio.create_subprocess_exec(sys.executable, '-c', CODEX, *argv, **kwargs)
    return SpawnerRunner(process_factory=factory), seen


def _run(tmp_path, runner):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=30, store=store))
    return result, store


requires_unprivileged_posix = pytest.mark.skipif(
    os.name != 'posix' or os.geteuid() == 0,
    reason='real file-mode enforcement requires an unprivileged POSIX user',
)


@requires_unprivileged_posix
def test_production_probe_command_lets_the_sandboxed_writer_commit(tmp_path, monkeypatch):
    runner, seen = _runner(tmp_path, monkeypatch)
    result, store = _run(tmp_path, runner)
    assert result['ok'] is True, result
    assert '--sandbox' in seen[0] and seen[0][seen[0].index('--sandbox') + 1] == 'workspace-write'
    assert store.leg_status('gpt-6.2-sol-medium')['state'] != 'down'


@requires_unprivileged_posix
def test_without_the_grant_the_commit_fails_and_books_nothing(tmp_path, monkeypatch):
    runner, seen = _runner(tmp_path, monkeypatch, drop_grant=True)
    result, store = _run(tmp_path, runner)
    assert result['ok'] is False and result['infra'] is True, result
    assert 'sandbox' in result['reason']
    assert store.leg_status('gpt-6.2-sol-medium')['state'] != 'down'


def _probe_with(tmp_path, monkeypatch, spawn, timeout_s=5):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **k: '/bin/codex')
    store = LegStateStore(tmp_path / 'legs.sqlite')

    async def cancel(job_id):
        pass
    runner = SimpleNamespace(spawn=spawn, cancel=cancel, jobs={})
    return asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=timeout_s, store=store)), store


def _job(**kw):
    return SimpleNamespace(**{'job_id': 'j', 'status': 'completed', 'exit_code': 0, 'stdout': '', 'stderr': '', **kw})


def test_sandbox_denial_reported_by_the_writer_is_a_probe_defect(tmp_path, monkeypatch):
    async def spawn(**kw):
        return _job(stderr='fatal: Unable to create .git/index.lock: Read-only file system')
    result, store = _probe_with(tmp_path, monkeypatch, spawn)
    assert result['ok'] is False and result['infra'] is True and 'sandbox' in result['reason'], result
    assert store.leg_status('gpt-6.2-sol-medium')['state'] != 'down'


def test_a_sandbox_denial_followed_by_a_timeout_is_still_a_probe_defect(tmp_path, monkeypatch):
    class Runner:
        def __init__(self):
            self._tasks, self.jobs, self.cancelled = {}, {}, []

        async def spawn(self, **kwargs):
            job = _job(status='running', exit_code=None, stderr='fatal: Unable to create \'.git/index.lock\': Read-only file system')
            self.jobs[job.job_id] = job
            self._tasks[job.job_id] = asyncio.ensure_future(asyncio.sleep(3600))
            return job

        async def cancel(self, job_id):
            self.cancelled.append(job_id)
            self.jobs[job_id].status = 'cancelled'
            self._tasks[job_id].cancel()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **k: '/bin/codex')
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = Runner()
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=1, store=store))
    assert runner.cancelled == ['j']
    assert result['ok'] is False and result['infra'] is True and 'sandbox' in result['reason'], result
    assert store.leg_status('gpt-6.2-sol-medium')['state'] != 'down'


def test_an_uncommitted_ok_file_alone_is_not_a_sandbox_denial(tmp_path, monkeypatch):
    async def spawn(**kw):
        (Path(kw['cwd']) / 'ok.txt').write_text('ok')
        return _job(stderr='')
    result, _ = _probe_with(tmp_path, monkeypatch, spawn)
    assert result['ok'] is False and not result['infra'] and result['reason'].startswith('no commit')


def test_a_quota_reply_after_the_file_write_still_books_the_limit(tmp_path, monkeypatch):
    async def spawn(**kw):
        (Path(kw['cwd']) / 'ok.txt').write_text('ok')
        return _job(exit_code=1, stderr='Error: usage limit reached')
    result, store = _probe_with(tmp_path, monkeypatch, spawn)
    assert result['ok'] is False and not result['infra'], result
    assert result['skipped'] == 'limited' and store.leg_status('gpt-6.2-sol-medium')['state'] == 'limited', result


def test_a_real_no_commit_still_books_the_leg_down(tmp_path, monkeypatch):
    async def spawn(**kw):
        return _job(stdout='done')
    result, _ = _probe_with(tmp_path, monkeypatch, spawn)
    assert result['ok'] is False and not result['infra'] and result['reason'].startswith('no commit')


NOISY = r'''
import sys, time
sys.stderr.write('fatal: Unable to create .git/index.lock: Read-only file system\n')
for n in range(350):
    sys.stderr.write(f'diagnostic line {n}\n')
sys.stderr.flush()
time.sleep(60)
'''


def test_a_denial_that_scrolled_out_of_the_live_tail_before_the_timeout_is_still_a_probe_defect(tmp_path, monkeypatch):
    """Through SpawnerRunner: a real child prints the denial, then 350 lines, then hangs until the probe times out."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **k: '/bin/codex')
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client', lambda *a, **k: ('codex', None))

    async def factory(exe, *args, **kwargs):
        return await asyncio.create_subprocess_exec(sys.executable, '-c', NOISY, **kwargs)
    runner = SpawnerRunner(process_factory=factory)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=4, store=store))
    assert any(e.get('event') == 'sandbox_denied' for job in runner.jobs.values() for e in job.events)
    assert result['ok'] is False and result['infra'] is True and 'sandbox' in result['reason'], result
    assert store.leg_status('gpt-6.2-sol-medium')['state'] != 'down'


PROSE = r'''
import sys, time
sys.stderr.write(sys.argv[1] + '\n')
for n in range(350):
    sys.stderr.write(f'diagnostic line {n}\n')
sys.stderr.flush()
time.sleep(60)
'''


@pytest.mark.parametrize('prose', ['I did not encounter a read-only sandbox, the commit task is simply slow.',
                                   'No error was caused by the read-only sandbox'])
def test_model_prose_about_a_read_only_sandbox_is_not_sandbox_evidence(tmp_path, monkeypatch, prose):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **k: '/bin/codex')
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client', lambda *a, **k: ('codex', None))

    async def factory(exe, *args, **kwargs):
        return await asyncio.create_subprocess_exec(sys.executable, '-c', PROSE, prose, **kwargs)
    runner = SpawnerRunner(process_factory=factory)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=4, store=store))
    assert not any(e.get('event') == 'sandbox_denied' for job in runner.jobs.values() for e in job.events)
    assert result['ok'] is False and result['infra'] is False and result['reason'].startswith('timeout'), result


@pytest.mark.parametrize('line, evidence', [
    ("fatal: Unable to create '/r/.git/index.lock': Read-only file system", True),
    ('Error: Read-only file system (os error 30)', True),
    ("OSError: [Errno 30] Read-only file system: 'ok.txt'", True),
    ("touch: cannot touch 'ok.txt': Read-only file system", True),
    ("fatal: Unable to create '/r/.git/index.lock': Read-only file system\r\n", True),
    ("fatal: Unable to create '/r/.git/index.lock': Read-only file system\r", True),
    ('I did not encounter a read-only sandbox,', False),
    ('Note: the read-only sandbox blocked nothing', False),
    ('No error was caused by the read-only sandbox', False),
    ('error: the read-only file system was not the problem', False),
    ('The sandbox was not blocked or denied', False),
])
def test_only_error_shaped_lines_are_sandbox_evidence(line, evidence):
    from plangraph.spawner.env_redact import sandbox_denial
    assert bool(sandbox_denial(line)) is evidence


CRLF = r'''
import sys, time
sys.stderr.buffer.write(b"fatal: Unable to create '.git/index.lock': Read-only file system\r\n")
for n in range(350):
    sys.stderr.buffer.write(b'diagnostic line %d\r\n' % n)
sys.stderr.buffer.flush()
time.sleep(60)
'''


def test_a_crlf_terminated_denial_is_still_a_probe_defect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **k: '/bin/codex')
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client', lambda *a, **k: ('codex', None))

    async def factory(exe, *args, **kwargs):
        return await asyncio.create_subprocess_exec(sys.executable, '-c', CRLF, **kwargs)
    runner = SpawnerRunner(process_factory=factory)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=4, store=store))
    assert any(e.get('event') == 'sandbox_denied' for job in runner.jobs.values() for e in job.events)
    assert result['ok'] is False and result['infra'] is True and 'sandbox' in result['reason'], result
    assert store.leg_status('gpt-6.2-sol-medium')['state'] != 'down'


def _lane_worktree(tmp_path):
    main = tmp_path / 'main'
    subprocess.run(['git', 'init', '-q', str(main)], check=True)
    subprocess.run(['git', '-C', str(main), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q',
                    '--allow-empty', '-m', 'base'], check=True)
    lane = tmp_path / 'lane'
    subprocess.run(['git', '-C', str(main), 'worktree', 'add', '-q', '-b', 'lane/x', str(lane)], check=True)
    return main, lane


def _spawn_writer(monkeypatch, lane):
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client', lambda *a, **k: ('codex', None))
    seen = []

    async def factory(exe, *args, **kwargs):
        seen.append(list(args))
        return await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass', **kwargs)

    async def go():
        runner = SpawnerRunner(process_factory=factory)
        await runner.spawn(prompt='p', role='implement', model_or_combo='gpt-6.2-sol-medium',
                           client='codex', cwd=str(lane))
        await asyncio.gather(*runner._tasks.values(), return_exceptions=True)
    asyncio.run(go())
    return seen


def test_a_plain_implement_spawn_in_a_lane_worktree_grants_the_git_common_dir_and_cargo_pool(tmp_path, monkeypatch):
    main, lane = _lane_worktree(tmp_path)
    target = tmp_path / 'cache' / 'cargo-target'
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    argv = _spawn_writer(monkeypatch, lane)[0]
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    granted = {Path(argv[i + 1]).resolve() for i, a in enumerate(argv) if a == '--add-dir'}
    assert granted == {(main / '.git').resolve(), target.parent.resolve()}, argv


@requires_unprivileged_posix
def test_the_preflight_refuses_an_unwritable_cargo_pool_before_launch(tmp_path, monkeypatch):
    from plangraph.routing_errors import SpawnRefused
    _, lane = _lane_worktree(tmp_path)
    cache = tmp_path / 'cache'
    cache.mkdir()
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(cache / 'cargo-target'))
    cache.chmod(0o555)
    try:
        with pytest.raises(SpawnRefused, match='not writable'):
            _spawn_writer(monkeypatch, lane)
    finally:
        cache.chmod(0o755)
