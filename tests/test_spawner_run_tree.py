"""Run-tree selection and launch provenance using local Git and fake children."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import asyncio
import io
from types import SimpleNamespace

import pytest

from plangraph.spawner import git_guard


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
        *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def trees(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    origin, repo, state = (tmp_path / name for name in ('origin', 'clone', 'state'))
    origin.mkdir()
    git(origin, 'init', '-b', 'main')
    (origin / 'version').write_text('A')
    git(origin, 'add', '.')
    git(origin, 'commit', '-m', 'tree A')
    git(tmp_path, 'clone', str(origin), str(repo))
    git(repo, 'switch', '-c', 'caller')
    return origin, repo, state


@pytest.fixture
def cached_tree(trees):
    with git_guard.run_tree(trees[2], trees[1]) as tree:
        assert git(trees[1], 'rev-parse', 'HEAD') == git(trees[0], 'rev-parse', 'HEAD')
        return *trees, tree


def advance(origin, version='B'):
    (origin / 'version').write_text(version)
    git(origin, 'commit', '-am', 'tree ' + version)
    return git(origin, 'rev-parse', 'HEAD')


def save_record(state, **row):
    record = state / 'runs/run-a/record.json'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(row))
    return record


def age(path):
    os.utime(path, (1, 1))
    return Path(path)


def test_live_run_keeps_a_new_run_selects_b(cached_tree):
    origin, repo, state, a = cached_tree
    caller = git(repo, 'rev-parse', 'HEAD')
    path = age(a['tree_path'])
    save_record(state, status='running', tree_sha=a['tree_sha'], ended=2)
    main = advance(origin)
    with git_guard.run_tree(state, repo) as b:
        assert b['tree_sha'] == main != a['tree_sha']
        assert Path(b['tree_path']) == state / 'trees' / main
        assert git(b['tree_path'], 'rev-parse', 'HEAD') == main
        assert git(b['tree_path'], 'branch', '--show-current') == ''
        assert (Path(b['tree_path']) / 'version').read_text() == 'B'
        assert (path / 'version').read_text() == 'A'
        assert git(path, 'rev-parse', 'HEAD') == caller
        assert path.stat().st_mtime == 1
        assert git(repo, 'rev-parse', 'HEAD') == caller


@pytest.mark.parametrize('registered', [False, True, 'locked'], ids=['removed', 'unlocked', 'locked'])
@pytest.mark.parametrize('offline', [False, True])
def test_missing_cached_directory_is_created_before_use(cached_tree, registered, offline):
    _, repo, state, previous = cached_tree
    path = Path(previous['tree_path'])
    if registered:
        if registered == 'locked':
            git(repo, 'worktree', 'lock', path)
        shutil.rmtree(path)  # Delete only this temporary tree, retaining registration.
        assert str(path) in git(repo, 'worktree', 'list', '--porcelain')
    else:
        git(repo, 'worktree', 'remove', str(path))
    assert not path.exists()
    assert (path.parent / (previous['tree_sha'] + '.lease')).exists()
    if offline:
        git(repo, 'remote', 'set-url', 'origin', str(repo / 'missing'))
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == previous['tree_sha']
        assert tree['tree_path'] == previous['tree_path']
        assert tree.get('tree_stale') == ('fetch_failed' if offline else None)
        assert git(path, 'rev-parse', 'HEAD') == tree['tree_sha']


@pytest.mark.parametrize('failure', ['error', 'timeout'])
def test_fetch_failure_prefers_verified_origin_main_over_newest_cache(cached_tree, monkeypatch, failure):
    origin, repo, state, a = cached_tree
    main = advance(origin)
    with git_guard.run_tree(state, repo):
        pass
    os.utime(a['tree_path'], (2_000_000_000, 2_000_000_000))
    fake = state / 'trees' / ('0' * 40)
    fake.mkdir()
    os.utime(fake, (2_100_000_000, 2_100_000_000))
    run = subprocess.run
    def fail_fetch(args, **kwargs):
        if 'fetch' in args:
            assert kwargs['timeout'] == 10
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(args, 10)
            raise subprocess.CalledProcessError(1, args)
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', fail_fetch)
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == main
        assert tree['tree_stale'] == ('fetch_timeout' if failure == 'timeout' else 'fetch_failed')
        assert git(tree['tree_path'], 'rev-parse', 'HEAD') == main


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
@pytest.mark.parametrize('row', [dict(status='running'), dict(status='running', tree_sha=None),
    dict(status='needs_input'), dict(status='orphaned'),
    dict(status='pending', ended=2), dict(status='failed', survivors=[123]), {}])
def test_unidentified_nonterminal_record_protects_every_tree(trees, row):
    origin, repo, state = trees
    paths = []
    save_record(state, **row)
    for version in ('B', 'C'):
        with git_guard.run_tree(state, repo) as tree:
            paths.append(age(tree['tree_path']))
        advance(origin, version)
    with git_guard.run_tree(state, repo):
        assert all(path.is_dir() for path in paths)
    save_record(state, status='completed')  # Terminal legacy records do not pin trees.
    with git_guard.run_tree(state, repo):
        assert all(not path.exists() for path in paths)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
@pytest.mark.parametrize('status', ['running', 'needs_input', 'orphaned', 'limited',
    'stalled', 'timed_out', 'terminated_after_answer', 'completed', 'failed', 'cancelled',
    'killed', 'dry_run', 'succeeded', 'timeout', 'refused', 'skipped'])
def test_cleanup_protects_live_records_and_startup_leases(trees, status):
    from plangraph.spawner.runner import is_terminal
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as a:
        path = age(a['tree_path'])
        advance(origin)
        with git_guard.run_tree(state, repo) as b:
            assert path.is_dir()  # A has no durable record yet, but holds its lease.
    b_path = age(b['tree_path'])
    save_record(state, status=status, tree_sha=a['tree_sha'])
    advance(origin, 'C')
    with git_guard.run_tree(state, repo):
        assert path.is_dir() is not is_terminal(status)
        assert not b_path.exists()
    save_record(state, status='completed', tree_sha=a['tree_sha'])
    with git_guard.run_tree(state, repo):
        assert not path.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
@pytest.mark.parametrize('unreadable', [False, True])
def test_corrupt_records_withhold_cleanup_but_not_selection(cached_tree, monkeypatch, unreadable):
    origin, repo, state, a = cached_tree
    path = age(a['tree_path'])
    main = advance(origin)
    record = save_record(state, status='running')
    record.write_text('{')
    read = Path.read_text
    def fail_read(current, *args, **kwargs):
        if current == record.resolve() and unreadable:
            raise PermissionError('cannot read record')
        return read(current, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', fail_read)
    with git_guard.run_tree(state, repo) as b:
        assert b['tree_sha'] == main and path.is_dir()


def test_run_tree_changelog_entry():
    root = Path(__file__).resolve().parents[1]
    entries = [root / 'CHANGELOG.md', *root.glob('changelog.d/*.md')]
    assert any('SPAWNER-RUN-TREE-S1-TREE-PER-SHA' in p.read_text(encoding='utf-8') for p in entries)


def test_parallel_creation_is_idempotent(trees):
    _, repo, state = trees
    barrier = threading.Barrier(2)
    def select(_):
        barrier.wait(timeout=10)
        with git_guard.run_tree(state, repo) as tree:
            return tree
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(select, range(2)))
    assert results[0] == results[1] and git(repo, 'worktree', 'list', '--porcelain').count('worktree ') == 2


def test_slow_creation_does_not_hold_a_host_wide_lock(trees, monkeypatch):
    origin, repo, state = trees
    entered, release = threading.Event(), threading.Event()
    first_sha = git(origin, 'rev-parse', 'HEAD')
    run = subprocess.run
    def slow_add(args, **kwargs):
        if 'worktree' in args and 'add' in args and args[-1] == first_sha:
            assert kwargs['timeout'] == 30
            entered.set()
            assert release.wait(15)
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', slow_add)
    def select():
        with git_guard.run_tree(state, repo) as tree:
            return tree
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(select)
        try:
            assert entered.wait(10)
            main = advance(origin)
            second = pool.submit(select).result(timeout=10)
            assert second['tree_sha'] == main
        finally:
            release.set()
        assert first.result()['tree_sha'] == first_sha


def test_existing_directory_must_have_the_selected_head(cached_tree):
    origin, repo, state, a = cached_tree
    main = advance(origin)
    git(repo, 'fetch', 'origin')
    git(a['tree_path'], 'checkout', '--detach', main)
    git(repo, 'remote', 'set-url', 'origin', str(repo / 'missing'))
    git(repo, 'update-ref', 'refs/remotes/origin/main', a['tree_sha'])
    with pytest.raises(RuntimeError, match='run_tree_invalid_head'):
        with git_guard.run_tree(state, repo):
            pytest.fail('wrong HEAD was admitted')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cleanup leases')
def test_cleanup_keeps_dirty_and_young_trees(cached_tree):
    origin, repo, state, a = cached_tree
    (Path(a['tree_path']) / 'untracked').touch()
    age(a['tree_path'])
    advance(origin)
    with git_guard.run_tree(state, repo) as b:
        pass
    advance(origin, 'C')
    with git_guard.run_tree(state, repo):
        assert Path(a['tree_path']).is_dir() and Path(b['tree_path']).is_dir()


def test_windows_selection_disables_reaping_without_importing_fcntl(cached_tree, monkeypatch):
    import builtins
    from types import SimpleNamespace
    origin, repo, state, a = cached_tree
    age(a['tree_path'])
    main = advance(origin)
    calls = []
    monkeypatch.setitem(__import__('sys').modules, 'msvcrt', SimpleNamespace(
        LK_LOCK=1, LK_UNLCK=0, locking=lambda fd, mode, count: calls.append(mode)))
    real_import = builtins.__import__
    def no_fcntl(name, *args, **kwargs):
        if name == 'fcntl':
            raise AssertionError('Windows imported fcntl')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_fcntl)
    monkeypatch.setattr(git_guard, 'os', SimpleNamespace(
        name='nt', environ=os.environ, utime=os.utime, fstat=os.fstat))
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == main and Path(tree['tree_path']).is_dir() and Path(a['tree_path']).is_dir()
    assert calls == [1, 0, 1, 0]  # fetch and creation locks; detached trees never lock local main


def test_cached_directory_cannot_be_an_unregistered_subdirectory(trees):
    _, repo, state = trees
    state = repo / 'state'
    sha = git(repo, 'rev-parse', 'HEAD')
    (state / 'trees' / sha).mkdir(parents=True)
    with pytest.raises(RuntimeError, match='run_tree_invalid'):
        with git_guard.run_tree(state, repo):
            pytest.fail('plain directory was admitted as a worktree')


@pytest.mark.parametrize('damage', ['dirty', 'diverged'])
def test_detached_tree_leaves_local_main_alone(cached_tree, damage):
    origin, repo, state, cached = cached_tree
    git(repo, 'worktree', 'remove', cached['tree_path'])
    git(repo, 'switch', 'main')
    if damage == 'dirty':
        (repo / 'version').write_text('local edit')
    else:
        git(repo, 'commit', '--allow-empty', '-m', 'local divergence')
    before = git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain')
    expected = advance(origin)
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == expected
        assert (Path(tree['tree_path']) / 'version').read_text() == 'B'
    assert (git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain')) == before


@pytest.mark.parametrize('events', [None, [], [{'event': 'existing'}]])
def test_fresh_launch_spec_adds_tree_event_when_child_events_are_null(trees, monkeypatch, capsys, events):
    from plangraph.spawner import launch_spec
    import sys
    _, repo, state = trees
    monkeypatch.setenv('PLANGRAPH_SPAWNER_REPO', str(repo))
    request = dict(run_id='run-' + 'a' * 32, job_id='job-test', role='review',
                   dry_run=False, leg={'id': 'fixture'}, prompt='review', card={},
                   cwd=str(repo), state=str(state))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    run = subprocess.run
    def child(args, **kwargs):
        if args[0] == sys.executable:
            path = Path(kwargs['cwd'])
            assert path.parent == state / 'trees'
            assert kwargs['env']['PYTHONPATH'] == str(path / 'src')
            return SimpleNamespace(returncode=0, stdout=json.dumps({'events': events, 'argv': ['fake']}))
        return run(args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', child)
    assert launch_spec.fresh_main() == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec['events'] == [*(events or []), dict(event='run_tree', tree_sha=git(repo, 'rev-parse', 'HEAD'),
                                  tree_path=str(state / 'trees' / git(repo, 'rev-parse', 'HEAD')))]


@pytest.mark.parametrize('injected', [False, True])
def test_mcp_default_equivalent_runner_keeps_fresh_trees(tmp_path, injected):
    from plangraph.spawner.mcp import MCPSpawnerServer
    from plangraph.spawner.runner import SpawnerRunner
    server = MCPSpawnerServer(runner=SpawnerRunner() if injected else None, state=tmp_path)
    assert server.service.fresh_tree is True


@pytest.mark.parametrize('failure', ['handshake', 'registration', 'result', 'wait', 'cancel', 'late_cancel', 'ok', 'empty_seats'])
def test_fresh_spawn_releases_context_once_even_after_task_creation(tmp_path, monkeypatch, failure):
    from plangraph.spawner.seat_spawn import fresh_spawn
    released, calls = [], []
    tree = dict(tree_sha='a' * 40, tree_path=str(tmp_path / 'tree'))
    class Context:
        def __enter__(self): return tree
        def __exit__(self, *args): released.append(True)
    def context(state): return Context()
    class Tasks(dict):
        def __setitem__(self, key, value):
            if failure == 'registration':
                raise RuntimeError('registration')
            super().__setitem__(key, value)
    class Child:
        returncode = None
        def __init__(self): self.exited = asyncio.Event()
        stdin = SimpleNamespace(write=lambda data: calls.append(json.loads(data)),
                                drain=lambda: asyncio.sleep(0), close=lambda: None)
        async def wait(self):
            if failure == 'late_cancel' and self.returncode is None:
                await self.exited.wait()
            self.returncode = 0
            if failure == 'wait':
                raise RuntimeError('wait')
        def kill(self):
            self.returncode = -9
            self.exited.set()
        @property
        def stdout(self):
            async def readline():
                if failure == 'handshake':
                    return b''
                return json.dumps({'seats': []} if failure == 'empty_seats' else
                                  dict(run_id='run-test', status='completed', exit_code=0, **tree)).encode()
            return SimpleNamespace(readline=readline)
    async def start(*args, **kwargs):
        assert kwargs['cwd'] == tree['tree_path']
        return Child()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', start)
    def result(run_id):
        if failure == 'result':
            raise RuntimeError('result')
        return dict(run_id=run_id, status='completed', **tree)
    service = SimpleNamespace(state=tmp_path, code_tree=context, tasks=Tasks(), result=result,
                              runner=SimpleNamespace(default_cwd=str(tmp_path)))
    async def run():
        task = asyncio.create_task(fresh_spawn(service, dict(card={}, prompt='review', role='review', wait_seconds=None)))
        if failure == 'cancel':
            await asyncio.sleep(0)
            task.cancel()
        if failure == 'late_cancel':
            while not service.tasks:
                await asyncio.sleep(0)
            task.cancel()
        if failure in ('ok', 'empty_seats'):
            record = await task
            assert record == ({'seats': []} if failure == 'empty_seats' else result('run-test'))
        else:
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await task
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
    asyncio.run(run())
    assert released == [True]
    if calls:
        assert calls[0]['_tree'] == tree


def test_empty_seats_cli_envelope_keeps_main_json_and_exit_code(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import cli
    class Out(io.StringIO):
        def close(self): pass
    service = SimpleNamespace(state=tmp_path, route=lambda *a, **k: None)
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    child = SimpleNamespace(stdin=Out(), stdout=io.StringIO('{"seats": []}\n'))
    monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **k: child)
    assert cli.main(['spawn', '--card', '{"estimate_tokens":1}', 'Implement change']) == 1
    assert json.loads(capsys.readouterr().out) == {'seats': []}


@pytest.mark.parametrize('entry', ['spawn', 'seats', 'mcp', 'supervise'])
@pytest.mark.parametrize('identity', [True, False])
def test_all_python_entry_records_have_tree_sha_or_reason(tmp_path, monkeypatch, entry, identity):
    from registry_route_util import _service
    from plangraph.spawner import cli
    from plangraph.spawner.mcp import MCPSpawnerServer
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    code = dict(tree_sha='a' * 40 if identity else None,
                tree_branch='detached' if identity else None)
    monkeypatch.setattr(git_guard, 'CODE_IDENTITY', code)
    service = _service(tmp_path, monkeypatch)
    chosen = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.1-sol-xhigh')
    service._route = lambda *a, **k: SpawnPlan((chosen,), (), 'test', (chosen,), ())
    options = dict(card={'estimate_tokens': 100}, prompt='fixture', role='review',
                   writer_families=['claude'], dry_run=True, wait_seconds=None)
    if entry == 'mcp':
        server = MCPSpawnerServer(runner=service.runner, state=tmp_path)
        server.service = service
        result = asyncio.run(server.call_tool('plangraph_agent_spawn', options))
    elif entry == 'supervise':
        class Out(io.StringIO):
            def close(self): pass
        monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
        options.pop('wait_seconds')
        monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(dict(options, state=str(tmp_path)))))
        output = Out()
        monkeypatch.setattr(cli.sys, 'stdout', output)
        assert cli.main(['--supervise']) == 0
        result = json.loads(output.getvalue())
    else:
        result = asyncio.run(service.spawn(**options, seats=entry == 'seats'))
    for row in result.get('seats', [result]):
        saved = service.result(row['run_id'])
        assert saved['tree_sha'] == code['tree_sha']
        assert saved.get('tree_absent') == (None if identity else
            'spawner code checkout identity unavailable at process startup')


def test_supervisor_handoff_stamps_selected_tree_not_parent_checkout(tmp_path, monkeypatch):
    from registry_route_util import _service
    from plangraph.spawner import cli
    class Out(io.StringIO):
        def close(self): pass
    service = _service(tmp_path, monkeypatch)
    tree = dict(tree_sha='b' * 40, tree_path=str(tmp_path / 'selected'))
    payload = dict(state=str(tmp_path), card={'estimate_tokens': 100}, prompt='fixture',
                   dry_run=True, _tree=tree, _admitted=True, _cwd=str(tmp_path))
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(payload)))
    output = Out()
    monkeypatch.setattr(cli.sys, 'stdout', output)
    assert cli.main(['--supervise']) == 0
    result = json.loads(output.getvalue())
    assert result['tree_sha'] == tree['tree_sha'] != git_guard.CODE_IDENTITY['tree_sha']
    assert service.result(result['run_id'])['tree_path'] == tree['tree_path']


@pytest.mark.parametrize('raw', ['?', '[]', ' ' * (16 * 1024 * 1024 + 1)])
def test_fresh_adapter_rejects_invalid_requests_before_selecting_a_tree(monkeypatch, capsys, raw):
    from plangraph.spawner import launch_spec
    import sys
    monkeypatch.setattr(sys, 'stdin', io.StringIO(raw))
    monkeypatch.setattr(git_guard, 'run_tree', lambda *a: pytest.fail('invalid request selected a tree'))
    assert launch_spec.fresh_main() == 1
    assert capsys.readouterr().err == 'launch-spec failed: invalid request (json)\n'
