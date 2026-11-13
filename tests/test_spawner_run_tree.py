"""Local launch.rs/runs.rs/runs_tests.rs/run_tree_tests.rs worker contracts;
SPAWNER-RUN-CHECKOUT-FOLLOWS-MAIN.md.
"""
import asyncio, json, os, subprocess, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from plangraph.spawner import git_guard
def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
        *args], stderr=subprocess.DEVNULL, text=True).strip()
@pytest.fixture
def trees(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.delenv('GIT_CONFIG_PARAMETERS', raising=False)
    origin, repo, state = (tmp_path / name for name in ('origin', 'clone', 'state'))
    origin.mkdir(); git(origin, 'init', '-b', 'main')
    package = origin / 'src/plangraph/spawner'
    package.mkdir(parents=True)
    for path in (package.parent / '__init__.py', package / '__init__.py'): path.touch()
    # Real CLI dispatch and supervisor; only its harness service is a fixture.
    source = Path(__file__).resolve().parents[1] / 'src/plangraph'
    (package / 'cli.py').write_bytes((source / 'spawner/cli.py').read_bytes())
    planner = package.parent / 'planner'; planner.mkdir()
    (planner / '__init__.py').touch()
    (planner / 'routing.py').write_text('class SpawnRefused(Exception): pass\nclass BenchmarkError(Exception): pass\n')
    support = package.parent / 'routing_support'; support.mkdir()
    (support / '__init__.py').write_text('def background_process_options(**kwargs): return {}\n')
    (package / 'runner.py').write_text('''import asyncio, json, os, pathlib, time
def redact_output(value): return value
class spawn_service:
    def __init__(self, state):
        self.state = pathlib.Path(state); self.tasks = {}; self.runner = self
    def _save(self, record):
        path = self.state / 'runs' / record['run_id'] / 'record.json'
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(record))
        return record
    async def spawn(self, prompt, card=None, **kwargs):
        assert self._parent_admitted is True
        if (card or {}).get('refuse'):
            from plangraph.planner.routing import SpawnRefused
            raise SpawnRefused('fixture refusal')
        record = self._save(dict(run_id=prompt, status='running', ended=None,
            worker_cwd=os.getcwd(), source=__file__, **(card or {})))
        async def finish():
            while not (self.state / ('finish-' + prompt)).exists(): await asyncio.sleep(.01)
            record.update(status='completed', ended=time.time()); self._save(record)
        self.tasks[prompt] = asyncio.create_task(finish())
        return record
    def ps(self): return {'runs': []}
    def save_refusal(self, exc, role):
        return self._save(dict(run_id='run-refused', status='refused', error=str(exc),
            refusal=type(exc).__name__, exit_code=2))
''')
    git(origin, 'add', '.'); git(origin, 'commit', '-m', 'tree A')
    git(tmp_path, 'clone', str(origin), str(repo))
    monkeypatch.setenv('PLANGRAPH_SPAWNER_REPO', str(repo))
    return origin, repo, state
def advance(origin):
    (origin / 'version').write_text('B')
    git(origin, 'add', '.'); git(origin, 'commit', '-m', 'tree B')
    return git(origin, 'rev-parse', 'HEAD')
def test_live_worker_keeps_a_new_worker_imports_b(trees):
    from types import SimpleNamespace
    origin, repo, state = trees
    service = SimpleNamespace(state=state, runner=SimpleNamespace(default_cwd=str(repo)), tasks={})
    async def run():
        a = await asyncio.wait_for(git_guard.fresh_spawn(service, {'prompt': 'run-a'}), 10)
        try:
            b_sha = advance(origin)
            b = await asyncio.wait_for(git_guard.fresh_spawn(service, {'prompt': 'run-b'}), 10)
            assert b['tree_sha'] == b_sha != a['tree_sha']
            for record in (a, b):
                tree = state / 'trees' / record['tree_sha']
                assert Path(record['worker_cwd']) == tree
                assert Path(record['source']).is_relative_to(tree)
            assert json.loads((state / 'runs/run-a/record.json').read_text())['tree_sha'] == a['tree_sha']
        finally:
            for name in ('run-a', 'run-b'): (state / ('finish-' + name)).touch()
            await asyncio.gather(*service.tasks.values())
        final = json.loads((state / 'runs/run-a/record.json').read_text())
        assert (final['status'], final['tree_sha']) == ('completed', a['tree_sha'])
    asyncio.run(run())
def test_parallel_creation_is_idempotent(trees):
    _, repo, state = trees
    barrier = threading.Barrier(2)
    def spawn():
        barrier.wait(timeout=5)
        with git_guard.run_tree(state, repo) as tree: return tree
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(lambda _: spawn(), range(2)))
    assert results[0] == results[1]
    assert len(git(repo, 'worktree', 'list', '--porcelain').split('worktree ')) == 3
@pytest.mark.parametrize('warm', [False, True])
def test_fetch_failure_uses_existing_tree_and_records_stale(trees, warm):
    _, repo, state = trees
    if warm:
        with git_guard.run_tree(state, repo) as previous: pass
    git(repo, 'remote', 'set-url', 'origin', str(repo / 'missing'))
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_stale'] == 'fetch_failed'
        assert tree['tree_sha'] == (previous['tree_sha'] if warm else git(repo, 'rev-parse', 'HEAD'))
        assert Path(tree['tree_path']).is_dir()
def test_fetch_failure_is_persisted_by_worker(trees):
    from types import SimpleNamespace
    _, repo, state = trees
    with git_guard.run_tree(state, repo): pass
    git(repo, 'remote', 'set-url', 'origin', str(repo / 'missing'))
    async def run():
        service = SimpleNamespace(state=state, tasks={})
        record = await git_guard.fresh_spawn(service, {'prompt': 'run-stale'})
        try:
            saved = json.loads((state / 'runs/run-stale/record.json').read_text())
            assert saved['tree_stale'] == record['tree_stale'] == 'fetch_failed'
        finally:
            (state / 'finish-run-stale').touch()
            await asyncio.gather(*service.tasks.values())
    asyncio.run(run())
def test_cleanup_protects_live_records_and_startup_leases(trees):
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as a:
        path = Path(a['tree_path']); os.utime(path, (1, 1))
        advance(origin)
        with git_guard.run_tree(state, repo): assert path.is_dir()  # startup lease, no record yet
    record = state / 'runs/run-a/record.json'
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(dict(a, status='running', ended=None)))
    with git_guard.run_tree(state, repo): assert path.is_dir()
    record.write_text(json.dumps(dict(a, status='completed', ended=2)))
    with git_guard.run_tree(state, repo): assert not path.exists()
def test_launch_adapter_uses_fresh_code_and_emits_tree_identity(trees, monkeypatch):
    origin, repo, state = trees
    package = origin / 'src/plangraph/spawner/launch_spec.py'
    package.write_text('import json, os\ndef main(stdin, stdout, stderr):\n'
                      ' request = json.load(stdin)\n'
                      ' stdout.write(json.dumps(dict(cwd=os.getcwd(), events=[], request=request)))\n'
                      ' return 0\n')
    git(origin, 'add', '.'); git(origin, 'commit', '-m', 'fresh launch adapter')
    from plangraph.spawner import launch_spec
    import io
    output = io.StringIO()
    request = dict(run_id='run-' + 'a' * 32, job_id='job', role='review', dry_run=True,
        leg={'id': 'fixture'}, prompt='task', card={}, cwd=str(repo), state=str(state))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(request)))
    monkeypatch.setattr('sys.stdout', output)
    assert launch_spec.fresh_main() == 0
    spec = json.loads(output.getvalue())
    assert spec['request'] == request
    event = spec['events'][-1]
    assert event['tree_sha'] == git(origin, 'rev-parse', 'HEAD')
    assert Path(spec['cwd']) == state / 'trees' / event['tree_sha']
def test_supervisor_stamps_every_record_without_rewriting_other_trees(tmp_path, monkeypatch):
    from plangraph.spawner import cli
    from types import SimpleNamespace
    import io
    saved = []
    class Service:
        tasks = {}
        runner = SimpleNamespace(default_cwd='task')
        def _save(self, row): saved.append(dict(row)); return row
        async def spawn(self, **kwargs):
            assert self.runner.default_cwd == str(tmp_path)
            self._save({'run_id': 'run-a'})
            return self._save({'run_id': 'run-b', 'tree_sha': 'other'})
    monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: Service())
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(dict(state=str(tmp_path),
        _tree={'tree_sha': 'selected'}, _cwd=str(tmp_path)))))
    class Output(io.StringIO):
        def close(self): pass
    monkeypatch.setattr('sys.stdout', Output())
    assert cli._supervise() == 0
    assert [r['tree_sha'] for r in saved] == ['selected', 'other']
def test_mcp_dispatch_uses_tree_without_restarting_server(trees):
    from plangraph.spawner.mcp import MCPSpawnerServer
    _, _, state = trees
    async def run():
        server = MCPSpawnerServer(state=state)
        runner = server.runner
        record = await server.call_tool('plangraph_agent_spawn',
            dict(card={'estimate_tokens': 100}, prompt='run-mcp'))
        assert record['tree_sha'] and server.runner is runner
        (state / 'finish-run-mcp').touch()
        await asyncio.gather(*server.service.tasks.values())
    asyncio.run(run())
def test_run_tree_design_and_changelog_name_the_evidence_boundary(trees):
    # Exercise the documented boundary: code selection neither waits for idle
    # runs nor fast-forwards the caller's checkout like the render-only updater.
    origin, repo, state = trees
    caller = git(repo, 'rev-parse', 'HEAD')
    with git_guard.run_tree(state, repo) as a: pass
    record = state / 'runs/run-boundary/record.json'; record.parent.mkdir(parents=True)
    record.write_text(json.dumps(dict(a, status='running', ended=None)))
    main = advance(origin)
    with git_guard.run_tree(state, repo) as b:
        assert b['tree_sha'] == main != caller
        assert Path(a['tree_path']).is_dir()
        assert git(repo, 'rev-parse', 'HEAD') == caller
        assert git(b['tree_path'], 'branch', '--show-current') == ''


@pytest.mark.parametrize('envelope', [{'seats': None}, {'seats': []},
    {'seats': [], 'writers': [{'run_id': 'run-envelope'}]}])
def test_empty_seats_registers_single_or_writer_task(trees, envelope):
    from types import SimpleNamespace
    _, _, state = trees
    async def run():
        service = SimpleNamespace(state=state, tasks={})
        result = await git_guard.fresh_spawn(service, {'prompt': 'run-envelope', 'card': envelope})
        try:
            assert service.tasks.keys() == {'run-envelope'}
            assert not service.tasks['run-envelope'].done()
            assert result['seats'] == envelope['seats']
        finally:
            (state / 'finish-run-envelope').touch()
            await asyncio.gather(*service.tasks.values())
    asyncio.run(run())


def test_child_refusal_keeps_spawn_refused(trees):
    from types import SimpleNamespace
    from plangraph.routing_errors import SpawnRefused
    _, _, state = trees
    with pytest.raises(SpawnRefused, match='fixture refusal'):
        asyncio.run(git_guard.fresh_spawn(SimpleNamespace(state=state, tasks={}),
            {'prompt': 'run-refused', 'card': {'refuse': True}}))
    assert json.loads((state / 'runs/run-refused/record.json').read_text())['refusal'] == 'SpawnRefused'


def test_windows_selection_spawns_without_fcntl_and_disables_reaping(trees, monkeypatch):
    import builtins
    from types import SimpleNamespace
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as old: pass
    old_path = Path(old['tree_path']); os.utime(old_path, (1, 1)); advance(origin)
    windows_os = SimpleNamespace(name='nt', environ=os.environ, utime=os.utime, getcwd=os.getcwd,
        fstat=os.fstat)
    calls = []
    msvcrt = SimpleNamespace(LK_LOCK=1, LK_UNLCK=0,
        locking=lambda fd, mode, count: calls.append((mode, count)))
    monkeypatch.setitem(__import__('sys').modules, 'msvcrt', msvcrt)
    real_import = builtins.__import__
    def no_fcntl(name, *args, **kwargs):
        if name == 'fcntl': raise AssertionError('Windows imported fcntl')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_fcntl)
    monkeypatch.setattr(git_guard, 'os', windows_os)
    with git_guard.run_tree(state, repo) as tree:
        assert tree['tree_sha'] == git(origin, 'rev-parse', 'HEAD')
        assert Path(tree['tree_path']).is_dir() and old_path.is_dir()
    assert calls == [(1, 1), (0, 1)]
    create = asyncio.create_subprocess_exec
    async def windows_child(*args, **kwargs):
        assert kwargs.pop('creationflags') == 0x08000200
        assert 'start_new_session' not in kwargs
        return await create(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', windows_child)
    async def spawn():
        service = SimpleNamespace(state=state, tasks={})
        record = await git_guard.fresh_spawn(service, {'prompt': 'run-windows'})
        assert record['tree_sha'] == tree['tree_sha']
        (state / 'finish-run-windows').touch()
        await asyncio.gather(*service.tasks.values())
    asyncio.run(spawn())
    assert calls == [(1, 1), (0, 1), (1, 1), (0, 1)]


@pytest.mark.parametrize('failure', ['dirty', 'stat'])
def test_cleanup_continues_after_one_tree_fails_and_unlinks_lease(trees, monkeypatch, failure):
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as a: pass
    advance(origin)
    with git_guard.run_tree(state, repo) as b: pass
    a_path, b_path = Path(a['tree_path']), Path(b['tree_path'])
    os.utime(a_path, (1, 1)); os.utime(b_path, (2, 2))
    (origin / 'third').touch(); git(origin, 'add', '.'); git(origin, 'commit', '-m', 'tree C')
    if failure == 'dirty':
        (b_path / 'untracked').touch()
    else:
        cleanup, stat, seen, faults = git_guard._cleanup_tree, Path.stat, [], []
        def fail_cleanup(old, *args):
            seen.append(old)
            with monkeypatch.context() as patch:
                def fail_stat(path, *a, **kw):
                    if path == b_path:
                        faults.append(path)
                        raise OSError('cannot read tree during cleanup')
                    return stat(path, *a, **kw)
                patch.setattr(Path, 'stat', fail_stat)
                return cleanup(old, *args)
        monkeypatch.setattr(git_guard, '_cleanup_tree', fail_cleanup)
    with git_guard.run_tree(state, repo): pass
    assert b_path.is_dir() and not a_path.exists()
    assert not (state / 'trees' / (a['tree_sha'] + '.lease')).exists()
    if failure == 'dirty': assert (state / 'trees' / (b['tree_sha'] + '.lease')).is_file()
    else:
        assert seen == [b_path, a_path]
        assert faults == [b_path]


@pytest.mark.parametrize('unreadable', [False, True])
def test_corrupt_records_withhold_cleanup_but_not_selection(trees, monkeypatch, unreadable):
    origin, repo, state = trees
    with git_guard.run_tree(state, repo) as a: pass
    path = Path(a['tree_path']); os.utime(path, (1, 1)); advance(origin)
    record = state / 'runs/run-corrupt/record.json'
    record.parent.mkdir(parents=True); record.write_text('{')
    with monkeypatch.context() as patch:
        if unreadable:
            read = Path.read_text
            def fail_read(current, *args, **kwargs):
                if current == record: raise PermissionError('cannot read record')
                return read(current, *args, **kwargs)
            patch.setattr(Path, 'read_text', fail_read)
        with git_guard.run_tree(state, repo) as b:
            assert b['tree_sha'] != a['tree_sha'] and path.is_dir()
    record.write_text(json.dumps(dict(a, status='completed', ended=2)))
    with git_guard.run_tree(state, repo): assert not path.exists()


@pytest.mark.parametrize('state_value', [None, '', 'relative'])
def test_fresh_adapter_rejects_missing_or_invalid_state_before_git(monkeypatch, capsys, state_value, tmp_path):
    import io
    from plangraph.spawner import launch_spec
    request = dict(run_id='run-' + 'a' * 32, job_id='job', role='review', dry_run=True,
        leg={'id': 'fixture'}, prompt='task', card={}, cwd=str(tmp_path))
    if state_value is not None: request['state'] = state_value
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(request)))
    def no_tree(*args): raise AssertionError('invalid request reached Git')
    monkeypatch.setattr(git_guard, 'run_tree', no_tree)
    assert launch_spec.fresh_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'launch-spec failed: invalid request (state)\n'
