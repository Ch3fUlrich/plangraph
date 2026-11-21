"""Run-tree selection and launch provenance using local Git and fake children."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import asyncio
import io
from types import SimpleNamespace

import pytest
from fake_pid import FAKE_PID

from plangraph.spawner import git_guard
from plangraph.routing_errors import SpawnRefused

pytestmark = pytest.mark.real_base_fresh


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


def test_cached_tree_behind_origin_after_selection_is_refused(cached_tree, monkeypatch):
    from contextlib import contextmanager
    origin, repo, state, cached = cached_tree
    lock = git_guard.run_worktree_lock
    @contextmanager
    def advance_before_cache_lock(root):
        if root == state / 'trees' / '.locks' / cached['tree_sha']:
            advance(origin)
            git(repo, 'fetch', 'origin')
        with lock(root):
            yield
    monkeypatch.setattr(git_guard, 'run_worktree_lock', advance_before_cache_lock)
    with pytest.raises(SpawnRefused, match='base_stale: behind=1'):
        with git_guard.run_tree(state, repo):
            pytest.fail('cached tree behind fetched origin/main was reused')
    assert git(cached['tree_path'], 'rev-parse', 'HEAD') == cached['tree_sha']


def test_current_cached_tree_reuses_admission_fetch(cached_tree, monkeypatch):
    _, repo, state, cached = cached_tree
    from plangraph.spawner.base_fresh import base_fresh
    run = subprocess.run
    fetches = []
    def count_fetch(args, **kwargs):
        if 'fetch' in args:
            fetches.append(args)
        return run(args, **kwargs)
    monkeypatch.setattr(git_guard.subprocess, 'run', count_fetch)
    assert base_fresh(repo, default='main')['ok']
    with git_guard.run_tree(state, repo, fetch=False) as tree:
        assert tree == cached
    assert len(fetches) == 1


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
def test_fresh_launch_spec_adds_tree_event_when_child_events_are_null(tmp_path, monkeypatch, capsys, events):
    from contextlib import nullcontext
    from plangraph.spawner import launch_spec
    import sys
    tree = dict(tree_sha='a' * 40, tree_path=str(tmp_path / 'tree'))
    monkeypatch.setattr(git_guard, 'run_tree', lambda state: nullcontext(tree))
    request = dict(run_id='run-' + 'a' * 32, job_id='job-test', role='review',
                   dry_run=False, leg={'id': 'fixture'}, prompt='review', card={},
                   cwd=str(tmp_path), state=str(tmp_path))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    seen = {}
    def run(*a, **k):
        seen.update(a[3])
        return SimpleNamespace(returncode=0, stdout=json.dumps({'events': events, 'argv': ['fake']}))
    monkeypatch.setenv('PYTHONPATH', '/host/site')
    monkeypatch.setattr(launch_spec, '_run_group', run)
    assert launch_spec.fresh_main() == 0
    assert seen['PYTHONPATH'] == os.pathsep.join([str(tmp_path / 'tree' / 'src'), '/host/site'])
    assert json.loads(capsys.readouterr().out)['events'] == [*(events or []), dict(event='run_tree', **tree)]


@pytest.mark.parametrize('failure', ['handshake', 'registration', 'result', 'wait', 'cancel', 'late_cancel', 'ok', 'empty_seats', 'seats', 'writers'])
def test_fresh_spawn_releases_context_once_even_after_task_creation(tmp_path, monkeypatch, failure):
    from plangraph.spawner.seat_spawn import fresh_spawn
    released, calls, killed = [], [], []
    tree = dict(tree_sha='a' * 40, tree_path=str(tmp_path / 'tree'))
    class Context:
        def __enter__(self): return tree
        def __exit__(self, *args): released.append(True)
    class Tasks(dict):
        def __setitem__(self, key, value):
            if failure == 'registration': raise RuntimeError('registration')
            super().__setitem__(key, value)
    row = {'seats': []} if failure == 'empty_seats' else dict(run_id='run-test', status='completed', **tree)
    if failure in ('seats', 'writers'): row = {failure: [dict(row, status='running')]}
    child = SimpleNamespace(returncode=None if failure == 'late_cancel' else 0,
        stdin=SimpleNamespace(write=lambda data: calls.append(json.loads(data)), close=lambda: None),
        stdout=io.BytesIO(b'' if failure == 'handshake' else json.dumps(row).encode()),
        kill=lambda: killed.append(True), wait=lambda: 0)
    def poll():
        if failure == 'wait': raise RuntimeError('wait')
        return child.returncode
    child.poll = poll
    from plangraph.spawner import cli
    monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **k: child)
    def result(run_id):
        if failure == 'result': raise RuntimeError('result')
        return dict(run_id=run_id, status='completed', **tree)
    service = SimpleNamespace(state=tmp_path, code_tree=lambda state: Context(), tasks=Tasks(), result=result,
                              runner=SimpleNamespace(default_cwd=str(tmp_path)))
    async def run():
        task = asyncio.create_task(fresh_spawn(service, dict(card={}, prompt='review', role='review', wait_seconds=None), admission_passed=True))
        if failure == 'cancel':
            await asyncio.sleep(0)
            task.cancel()
        if failure == 'late_cancel':
            while not service.tasks: await asyncio.sleep(0)
            task.cancel()
        if failure in ('ok', 'empty_seats', 'seats', 'writers'):
            assert await task == ({'seats': []} if failure == 'empty_seats' else {failure: [result('run-test')]} if failure in ('seats', 'writers') else result('run-test'))
        else:
            with pytest.raises((RuntimeError, ValueError, asyncio.CancelledError)): await task
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
    asyncio.run(run())
    assert released == [True]
    assert not killed  # A cancelled caller must not kill an admitted supervisor.
    if calls: assert calls[0]['_tree'] == tree


def test_handshake_timeout_kills_the_supervisor_group(tmp_path, monkeypatch):
    from plangraph.spawner import cli, base_fresh
    from plangraph.spawner.seat_spawn import fresh_spawn
    killed, groups = [], []
    tree = dict(tree_sha='a' * 40, tree_path=str(tmp_path / 'tree'))
    class Context:
        def __enter__(self): return tree
        def __exit__(self, *args): pass
    child = SimpleNamespace(pid=FAKE_PID, stdin=SimpleNamespace(write=lambda data: None, close=lambda: None),
        stdout=SimpleNamespace(readline=lambda: time.sleep(.3) or b'', close=lambda: None),
        poll=lambda: None, kill=lambda: killed.append(True), wait=lambda: 0)
    monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **k: child)
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: groups.append(pid))
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', -4.9)
    service = SimpleNamespace(state=tmp_path, code_tree=lambda state: Context(), tasks={},
                              runner=SimpleNamespace(default_cwd=None))
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(fresh_spawn(service, dict(card={}, prompt='review', role='review'), admission_passed=True))
    assert groups == [FAKE_PID] and not killed


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
    from plangraph.spawner import cli, mcp
    from plangraph.planner import routing, legs
    code = dict(tree_sha='a' * 40 if identity else None, tree_branch='detached' if identity else None)
    monkeypatch.setattr(git_guard, 'CODE_IDENTITY', code)
    service = _service(tmp_path, monkeypatch)
    chosen = next(l for l in legs.DEFAULT_LEGS if l.id == 'cx-gpt-6.2-sol-medium')
    service._route = lambda *a, **k: routing.SpawnPlan((chosen,), (), 'test', (chosen,), ())
    options = dict(card={'estimate_tokens': 100}, prompt='fixture', role='review',
                   writer_families=['claude'], dry_run=True)
    if entry == 'mcp':
        server = mcp.MCPSpawnerServer(runner=service.runner, state=tmp_path)
        server.service = service
        result = asyncio.run(server.call_tool('plangraph_agent_spawn', options))
    elif entry == 'supervise':
        output = io.StringIO()
        output.close = lambda: None
        monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
        monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(dict(options, state=str(tmp_path)))))
        monkeypatch.setattr(cli.sys, 'stdout', output)
        assert cli.main(['--supervise']) == 0
        result = json.loads(output.getvalue())
    else: result = asyncio.run(service.spawn(**options, seats=entry == 'seats'))
    assert (rows := result.get('seats', [result]))
    for row in rows:
        saved = service.result(row['run_id'])
        assert saved['tree_sha'] == code['tree_sha']
        assert saved.get('tree_absent') == (None if identity else 'spawner code checkout identity unavailable at process startup')
    monkeypatch.setattr(git_guard, 'CODE_TREE', tmp_path)
    monkeypatch.delenv('PLANGRAPH_SPAWNER_REPO', raising=False)
    assert not cli.live_runner(mcp.SpawnerRunner())


@pytest.mark.parametrize('entry', ['cli', 'cli-wait', 'mcp'])
def test_live_spawn_survives_parent_exit_with_selected_tree_and_supervisor_evidence(trees, monkeypatch, entry, request):
    import inspect, time
    from plangraph.spawner import cli, proc_kill
    origin, repo, state = trees
    request.addfinalizer(lambda: (state / 'finish').touch())
    package = origin / 'src/plangraph/spawner'
    shutil.copytree(Path(git_guard.__file__).parents[1], package.parent)
    (package / 'runner.py').write_text('''from pathlib import Path
from types import SimpleNamespace
from .private_io import atomic_write_private
class Service:
    def __init__(self, state): self.state, self.runner = Path(state), SimpleNamespace(default_cwd=None)
    def _save(self, record):
        from .git_guard import annotate_tree
        annotate_tree(record)
        if record.get('tree_sha') is None: record.setdefault('tree_absent', 'unavailable')
        atomic_write_private(self.state / 'runs/run-fixture/record.json', __import__('json').dumps(record))
spawn_service = Service
''')
    (package / 'cli.py').write_text('''import json, os, pathlib, sys, time
from .runner import spawn_service as SpawnService
from .env_redact import redact_output
''' + inspect.getsource(cli._drain_chunks) + '''
def main(argv):
    payload = json.loads(sys.stdin.readline())
    assert not any(key.startswith('_') for key in payload)
    service = SpawnService(payload['state'])
    assert service._parent_admitted
    record = dict(run_id='run-fixture', status='running', base_admitted=True, tree_sha=None, tree_absent='forged',
                  cwd=payload.get('cwd') or service.runner.default_cwd, version=pathlib.Path('version').read_text(), pythonpath=os.environ['PYTHONPATH'])
    service._save(record)
    assert pathlib.Path(__file__).resolve().is_relative_to(pathlib.Path(record['tree_path']))
    print(json.dumps(record), flush=True)
    sys.stdout.close()
    sys.stderr.write('x' * (2 * 1024 * 1024) + '\\n' + os.environ['TEST_SERVICE_TOKEN'] + '\\n'); sys.stderr.flush()
    while not (service.state / 'finish').exists(): time.sleep(.01)
    service._save(dict(record, status='completed'))
    return 0
if __name__ == '__main__': raise SystemExit(main(sys.argv[1:]))
''')
    git(origin, 'add', 'src')
    sha = advance(origin)
    monkeypatch.delenv('PLANGRAPH_SUPERVISOR_SCOPE')   # this test runs the real scoped supervisor
    monkeypatch.setenv('PLANGRAPH_SPAWNER_REPO', str(repo))
    monkeypatch.setenv('TEST_SERVICE_TOKEN', 'fixture-secret-do-not-log')
    monkeypatch.setenv('PYTHONPATH', '/host-deps')
    driver = '''import asyncio, json, sys
from plangraph.spawner import cli
from plangraph.spawner.seat_spawn import SeatSpawnMixin
SeatSpawnMixin.admit_spawn = lambda *a, **k: None
if sys.argv[1].startswith('cli'):
    raise SystemExit(cli.main(['--state', sys.argv[2], 'spawn', '--wait', '0.02' if sys.argv[1] == 'cli-wait' else '0', '--card', '{"estimate_tokens":100}', '--cwd', sys.argv[3], '--role', 'review', '--exclude-family', 'openai', '--leg', 'cx-gpt-6.2-sol-medium', '--override-reason', 'fixture', 'Review fixture']))
from plangraph.spawner.mcp import MCPSpawnerServer
from plangraph.spawner.runner import SpawnerRunner
server = MCPSpawnerServer(SpawnerRunner(default_cwd=sys.argv[3]), state=sys.argv[2])
print(json.dumps(asyncio.run(server.call_tool('plangraph_agent_spawn', dict(card={'estimate_tokens':100}, prompt='Review fixture', role='review', writer_families=['openai'])))))
'''
    result = subprocess.run([sys.executable, '-c', driver, entry, str(state), str(repo)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'PYTHONPATH': os.pathsep.join(
                                filter(None, [str(Path(cli.__file__).parents[2]), os.environ.get('PYTHONPATH')]))})
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record['tree_sha'] == sha != git(repo, 'rev-parse', 'HEAD')
    assert record['version'] == 'B' and record['cwd'] == str(repo)
    assert record['pythonpath'].endswith(os.pathsep + '/host-deps')
    log = Path(record['supervisor_stderr_path'])
    pid = int(Path(str(log) + '.pid').read_text())
    if sys.platform.startswith('linux'): assert record['supervisor_pid_start'] == proc_kill._stat(pid)[4]
    assert record['supervisor_pid'] == pid   # the record names its owner process
    if sys.platform.startswith('linux') and cli.scope_argv(['x'])[0] == 'systemd-run':
        # The supervisor runs in a scope of its own, so stopping the caller's session cannot take the run along.
        assert Path(f'/proc/{pid}/cgroup').read_text() != Path('/proc/self/cgroup').read_text()
        assert Path(f'/proc/{pid}/cgroup').read_text().strip().endswith('.scope')
    (state / 'finish').touch()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        saved = json.loads((state / 'runs/run-fixture/record.json').read_text())
        if saved['status'] == 'completed' and '[autoos:redacted]' in log.read_text(): break
        time.sleep(.02)
    assert saved['status'] == 'completed' and '[autoos:redacted]' in log.read_text() and 'fixture-secret-do-not-log' not in log.read_text() and log.stat().st_size <= 1024 * 1024 and Path(str(log) + '.1').stat().st_size <= 1024 * 1024


def test_fresh_spawn_refuses_handoff_without_parent_admission(tmp_path):
    from plangraph.spawner.seat_spawn import fresh_spawn
    service = SimpleNamespace(state=tmp_path, code_tree=lambda state: pytest.fail('tree acquired'), tasks={})
    with pytest.raises(RuntimeError, match='fresh_spawn_requires_parent_admission'):
        asyncio.run(fresh_spawn(service, dict(card={}, prompt='review', role='review')))


def test_launch_spec_group_runner_kills_group_on_timeout(tmp_path):
    from plangraph.spawner import launch_spec
    with pytest.raises(subprocess.TimeoutExpired):
        launch_spec._run_group([sys.executable, '-c', 'import time; time.sleep(30)'], '', str(tmp_path), dict(os.environ), timeout=.3)


def test_fresh_launch_spec_reports_non_json_stdout(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext
    from plangraph.spawner import launch_spec
    tree = dict(tree_sha='a' * 40, tree_path=str(tmp_path / 'tree'))
    monkeypatch.setattr(git_guard, 'run_tree', lambda state: nullcontext(tree))
    request = dict(run_id='run-' + 'a' * 32, job_id='job-test', role='review', dry_run=False,
                   leg={'id': 'fixture'}, prompt='review', card={}, cwd=str(tmp_path), state=str(tmp_path))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    monkeypatch.setattr(launch_spec, '_run_group', lambda *a: SimpleNamespace(returncode=0, stdout='noise\n{}'))
    assert launch_spec.fresh_main() == 1
    assert 'non-JSON stdout' in capsys.readouterr().err
