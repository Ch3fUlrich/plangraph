"""Local launch.rs/runs.rs/runs_tests.rs workers; SPAWNER-RUN-CHECKOUT-FOLLOWS-MAIN.md."""
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
    # A real per-run Python worker witnesses its imported code, cwd and lifetime.
    (package / 'cli.py').write_text('''import json, os, pathlib, sys, time
p = json.loads(sys.stdin.readline())
tree = p.pop('_tree')
state = pathlib.Path(p['state'])
record = dict(tree, run_id=p['prompt'], status='running', ended=None,
              worker_cwd=os.getcwd(), source=__file__)
path = state / 'runs' / record['run_id'] / 'record.json'
path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(record))
print(json.dumps(record), flush=True)
while not (state / ('finish-' + record['run_id'])).exists():
    time.sleep(.01)
record.update(status='completed', ended=time.time()); path.write_text(json.dumps(record))
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
        a = await git_guard.fresh_spawn(service, {'prompt': 'run-a'})
        try:
            b_sha = advance(origin)
            b = await git_guard.fresh_spawn(service, {'prompt': 'run-b'})
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
    package = origin / 'src/plangraph/spawner/cli.py'
    package.write_text('import json, os, sys\nrequest = json.load(sys.stdin)\n'
                       'print(json.dumps(dict(cwd=os.getcwd(), events=[], request=request)))\n')
    git(origin, 'add', '.'); git(origin, 'commit', '-m', 'fresh launch adapter')
    from plangraph.spawner import launch_spec
    import io
    output = io.StringIO()
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'state': str(state)})))
    monkeypatch.setattr('sys.stdout', output)
    assert launch_spec.fresh_main() == 0
    spec = json.loads(output.getvalue())
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
def test_run_tree_design_and_changelog_name_the_evidence_boundary():
    root = Path(__file__).resolve().parents[1]
    for name in ('docs/SPAWNER.md', 'changelog.d/SPAWNER-RUN-CHECKOUT-FOLLOWS-MAIN.md'):
        text = (root / name).read_text()
        assert 'src/plangraph/live_follow.py:_follow' in text
        assert 'empty-run-list' in text and 'tree_sha' in text
