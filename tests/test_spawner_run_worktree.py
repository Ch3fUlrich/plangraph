"""RUN-WORKTREE slice 1: local Git only; never launch an agent or contact a host."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from plangraph.spawner import git_guard


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test Writer',
        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
        *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.delenv('GIT_CONFIG_PARAMETERS', raising=False)
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    origin, repo = tmp_path / 'origin', tmp_path / 'clone'
    origin.mkdir()
    git(origin, 'init', '-b', 'main')
    git(origin, 'commit', '--allow-empty', '-m', 'initial')
    git(tmp_path, 'clone', str(origin), str(repo))
    git(origin, 'commit', '--allow-empty', '-m', 'new main head')
    return origin, repo, tmp_path / 'worktrees'


def test_new_lane_fetches_main_not_callers_head(repository):
    origin, repo, root = repository
    git(repo, 'switch', '-c', 'caller')
    git(repo, 'commit', '--allow-empty', '-m', 'unrelated caller work')
    result = git_guard.create(root, repo, 'lane/new', False)
    assert result == dict(path=result['path'], branch='lane/new', base_sha=git(origin, 'rev-parse', 'HEAD'))
    assert Path(result['path']).parent == root
    assert git(result['path'], 'rev-parse', 'HEAD') == result['base_sha']
    assert git(result['path'], 'branch', '--show-current') == 'lane/new'
    assert (root / '.lock').is_file()


def test_continuation_resumes_tip_in_fresh_directory(repository):
    origin, repo, root = repository
    first = git_guard.create(root, repo, 'lane/resume', False)
    git(first['path'], 'commit', '--allow-empty', '-m', 'lane progress')
    tip = git(first['path'], 'rev-parse', 'HEAD')
    git(origin, 'commit', '--allow-empty', '-m', 'main advances independently')
    resumed = git_guard.create(root, repo, 'lane/resume', True)
    assert resumed['path'] != first['path'] and Path(first['path']).is_dir()
    assert resumed['base_sha'] == tip == git(resumed['path'], 'rev-parse', 'HEAD')
    assert git(resumed['path'], 'branch', '--show-current') == 'lane/resume'


def test_finished_dependency_must_be_on_main(repository):
    origin, repo, root = repository
    git(repo, 'switch', '-c', 'dependency')
    git(repo, 'commit', '--allow-empty', '-m', 'not merged')
    sha = git(repo, 'rev-parse', 'HEAD')
    from plangraph.routing_errors import SpawnRefused
    with pytest.raises(SpawnRefused, match='run_worktree_dependency_not_merged'):
        git_guard.create(root, repo, 'lane/refused', False, finished_dependencies=[sha])
    with pytest.raises(SpawnRefused, match='run_worktree_dependency_not_merged'):
        git_guard.create(root, repo, 'lane/missing', False, finished_dependencies=['0' * 40])
    assert not git(repo, 'branch', '--list', 'lane/refused')
    merged = git_guard.create(root, repo, 'lane/admitted', False,
                             finished_dependencies=[git(origin, 'rev-parse', 'HEAD')])
    assert Path(merged['path']).is_dir()


def test_concurrent_creates_hold_exact_shared_lock(repository, monkeypatch):
    _, repo, root = repository
    run = subprocess.run
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    creates = []
    def observed(args, **kwargs):
        if 'worktree' in args and 'add' in args:
            creates.append(args)
            if len(creates) == 1:
                entered.set()
                assert release.wait(10)
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', observed)
    def second():
        second_started.set()
        return git_guard.create(root, repo, 'lane/two', False)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(git_guard.create, root, repo, 'lane/one', False)
        try:
            assert entered.wait(10)
            two = pool.submit(second)
            assert second_started.wait(10)
            if os.name != 'nt':
                import fcntl
                with (root / '.lock').open('rb') as lock:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert len(creates) == 1
        finally:
            release.set()
        assert one.result()['path'] != two.result()['path']
    assert len(creates) == 2


@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('enabled', [None, False, True])
@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('configured_root', [False, True])
def test_run_record_and_runner_cwd(repository, tmp_path, monkeypatch, hardened, enabled, dry_run, configured_root):
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner import resolver, seat_spawn
    origin, repo, configured = repository
    git(repo, 'fetch', 'origin', 'main')
    git(repo, 'merge', '--ff-only', 'origin/main')
    root = configured if configured_root else tmp_path / 'state' / 'worktrees'
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 1000000))
    async def available(*a, **k):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', available)
    monkeypatch.setattr(resolver, 'plan_for_launch_async', lambda plan, **k: async_value(plan))
    calls = []
    class Process:
        pid = None
        returncode = 0
        async def communicate(self, input=None):
            return (b'{"type":"text","part":{"text":"finished"}}\n'
                    b'{"type":"step_finish","part":{"reason":"stop"}}\n'), b''
    async def factory(*args, **kwargs):
        if enabled:
            record_path = next((tmp_path / 'state' / 'runs').glob('*/record.json'))
            assert json.loads(record_path.read_text())['run_worktree']['path'] == kwargs['cwd']
        calls.append(kwargs)
        return Process()
    async def execute():
        runner = SpawnerRunner(default_cwd=str(repo), process_factory=factory)
        service = (spawn_service if hardened else SpawnService)(runner=runner, state=tmp_path / 'state')
        # D-5206: Sol xhigh is the routed default; plain Sol is now explicit-only.
        chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'cx-gpt-6.1-sol-xhigh')
        service._route = lambda *a, **k: SpawnPlan((chosen,), (), 'test', (), ())
        if configured_root:
            settings = tmp_path / 'settings.json'
            settings.write_text(json.dumps({'worktree_root': str(root)}))
            monkeypatch.setenv('PLANGRAPH_SETTINGS', str(settings))
        result = await service.spawn(card={'estimate_tokens': 100, 'lane_branch': 'lane/record'},
            prompt='Implement a tested change', dry_run=dry_run, wait_seconds=None,
            **({} if enabled is None else {'run_worktree': enabled}))
        assert result['status'] == ('dry_run' if dry_run else 'completed'), result.get('error')
        assert service.result(result['run_id']) == result
        expected = str(repo)
        if enabled and not dry_run:
            metadata = result['run_worktree']
            expected = metadata['path']
            assert Path(expected).parent == root
            assert metadata['branch'] == 'lane/record'
            assert metadata['base_sha'] == git(origin, 'rev-parse', 'HEAD')
            assert result['worktree_attempts'] == [metadata]
        else:
            assert 'run_worktree' not in result and not root.exists()
            assert not (service.state / 'worktrees').exists()
        assert result['cwd'] == expected
        if not dry_run:
            assert calls[0]['cwd'] == calls[0]['env']['PWD'] == expected
        assert runner.jobs[result['job_id']].cwd == expected
    asyncio.run(execute())


async def async_value(value):
    return value


def test_default_root_and_declared_continuation(repository, tmp_path):
    from types import SimpleNamespace
    _, repo, _ = repository
    saved = []
    service = SimpleNamespace(state=tmp_path / 'state', runner=SimpleNamespace(default_cwd=str(repo)),
                              _save=lambda record: saved.append(json.loads(json.dumps(record))))
    async def execute():
        record = {'run_id': 'run-first'}
        await git_guard.prepare_worktree(service, record, {}, True)
        first = record['run_worktree']
        assert Path(first['path']).parent == service.state / 'worktrees'
        git(first['path'], 'commit', '--allow-empty', '-m', 'progress')
        tip = git(first['path'], 'rev-parse', 'HEAD')
        child = {'run_id': 'run-second'}
        await git_guard.prepare_worktree(service, child,
            {'lane_branch': first['branch'], 'continuation': True}, True)
        assert child['run_worktree']['base_sha'] == tip
        assert child['run_worktree']['path'] != first['path']
        assert saved[-1]['cwd'] == child['run_worktree']['path']
        await git_guard.prepare_worktree(service, child, {}, True)
        assert len(child['worktree_attempts']) == 2
        assert len({m['path'] for m in child['worktree_attempts']}) == 2
    asyncio.run(execute())


def test_cli_flag_is_opt_in(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import cli
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    # D-5206: use the promoted default, not the explicit-only plain Sol leg.
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'cx-gpt-6.1-sol-xhigh')
    monkeypatch.setattr(SpawnService, '_route', lambda *a, **k: SpawnPlan((chosen,), (), 'test', (), ()))
    for flag in ([], ['--run-worktree']):
        assert cli.main(['--state', str(tmp_path), 'spawn', '--card', '{"estimate_tokens":100}',
            '--dry-run', 'Implement a tested change', *flag]) == 0
        record = json.loads(capsys.readouterr().out)
        assert record['status'] == 'dry_run' and 'run_worktree' not in record
        assert not (tmp_path / 'worktrees').exists()


@pytest.mark.parametrize('enabled', [None, False, True])
def test_cli_overflow_forwards_worktree_flag(repository, tmp_path, monkeypatch, capsys, enabled):
    import io
    from plangraph.spawner import cli, overflow, seat_spawn
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    _, repo, _ = repository
    git(repo, 'fetch', 'origin', 'main')
    git(repo, 'merge', '--ff-only', 'origin/main')
    monkeypatch.chdir(repo)
    config = tmp_path / 'overflow.json'
    config.write_text(json.dumps({'public_repos': [str(repo)], 'hosts': {'invented': {}}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 4000))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'local_limits', lambda *a: (2048, 3000))
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {'invented': dict(
        ssh='invented', ok=True, fresh=True, live_runs=0, mem_available_mb=6000, role_mb=2048, floor_mb=3000)})
    monkeypatch.setenv('PLANGRAPH_HOST', 'invented-local')
    # D-5206: overflow forwards the worktree flag with the new Sol xhigh default.
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'cx-gpt-6.1-sol-xhigh')
    monkeypatch.setattr(SpawnService, '_route', lambda *a, **k: SpawnPlan((chosen,), (), 'test', (), ()))
    received = []
    def remote(name, host, card, prompt, flags):
        received.append(flags)
        return dict(run_id='run-remote', status='running', exit_code=0)
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    class Input(io.StringIO):
        def close(self):
            pass
    class Supervisor:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stdout = Input(), self
        def readline(self):
            payload = json.loads(self.stdin.getvalue())
            service = cli.SpawnService(state=payload.pop('state'))
            assert payload['run_worktree'] == (enabled is True)
            return json.dumps(asyncio.run(service.spawn(**payload))) + '\n'
        def close(self):
            pass
    monkeypatch.setattr(cli, '_start_supervisor', Supervisor)
    flags = ['--run-worktree'] if enabled else []
    assert cli.main(['--state', str(tmp_path / 'state'), 'spawn', '--card', '{"estimate_tokens":100}',
                     'Implement a tested change', *flags]) == 0
    assert json.loads(capsys.readouterr().out)['overflow_remote']
    assert len(received) == 1
    assert ('--run-worktree' in received[0]) == (enabled is True)


def test_fixer_continuation_records_new_worktree(repository, tmp_path):
    from types import SimpleNamespace
    from plangraph.spawner.service import SpawnService
    from plangraph.spawner.runner import JobInfo
    _, repo, root = repository
    first = git_guard.create(root, repo, 'lane/fixer', False)
    git(first['path'], 'commit', '--allow-empty', '-m', 'parent result')
    tip = git(first['path'], 'rev-parse', 'HEAD')
    calls = []
    async def spawn(**kwargs):
        calls.append(kwargs)
        return JobInfo('child', 'completed', kwargs['prompt'], 'test', exit_code=0)
    service = SpawnService(runner=SimpleNamespace(default_cwd=str(repo), spawn=spawn, _tasks={}),
                           state=tmp_path / 'state')
    record = dict(run_id='run-parent', role='implement', status='completed', exit_code=0,
                  cwd=first['path'], run_worktree=first,
                  chosen_leg={'model': 'test', 'harness': 'opencode'})
    asyncio.run(service._continue_writer(record, 'Remaining gaps: finish the test'))
    child = service.result(record['continuation_run_id'])
    assert child['run_worktree']['branch'] == 'lane/fixer'
    assert child['run_worktree']['base_sha'] == tip
    assert child['cwd'] == calls[0]['cwd'] != first['path']
    assert child['worktree_attempts'] == [child['run_worktree']]
