"""D-5661: real local origins, fake harnesses, fail-closed admission."""
import asyncio
import io
import json
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.real_base_fresh
GIT_ENV = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
        'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', *args],
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    for key in (*GIT_ENV, 'GIT_CONFIG_PARAMETERS'):
        monkeypatch.delenv(key, raising=False)
    origin, repo = tmp_path / 'origin.git', tmp_path / 'repo'
    git(tmp_path, 'init', '--bare', '-b', 'main', str(origin))
    git(tmp_path, 'clone', str(origin), str(repo))
    git(repo, 'commit', '--allow-empty', '-m', 'initial')
    git(repo, 'push', 'origin', 'HEAD:main')
    return origin, repo
def advance(origin, repo):
    old = git(repo, 'rev-parse', 'HEAD')
    upstream = repo.parent / 'upstream'
    git(repo.parent, 'clone', str(origin), str(upstream))
    for _ in range(2):
        (upstream / 'change.txt').write_text(str(_))
        git(upstream, 'add', 'change.txt')
        git(upstream, 'commit', '--allow-empty', '-m', 'advance')
    git(upstream, 'push', 'origin', 'HEAD:main')
    return old
@pytest.fixture
def dispatch(repository, tmp_path, monkeypatch):
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    calls = []
    class Process:
        pid, returncode = None, 0
        async def communicate(self, input=None):
            return b'{"type":"text","part":{"text":"VERDICT: ACCEPT"}}\n', b''
    async def factory(*args, **kwargs):
        calls.append(kwargs)
        return Process()
    async def ready(*args, **kwargs):
        return None
    async def planned(plan, **kwargs):
        return plan
    for name in ('resolver.host_skip_reason_async', 'leg_probe.before_launch'):
        monkeypatch.setattr('plangraph.spawner.' + name, ready)
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', planned)
    monkeypatch.setattr('plangraph.spawner.seat_spawn.resources', lambda service: (0, 1000000))
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'cx-gpt-6.1-sol-xhigh')
    def create(hardened):
        runner = SpawnerRunner(default_cwd=str(repository[1]), process_factory=factory)
        service = (spawn_service if hardened else SpawnService)(runner=runner, state=tmp_path / 'state')
        service._route = lambda *a, **k: SpawnPlan((chosen,), (), 'test', (), ())
        return service
    return create, calls
@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('role', ['implement', 'orchestrate', 'research', 'writer', 'fix', 'review', 'judge'])
@pytest.mark.parametrize('decision', [None, 'D-123', 'invalid'])
def test_every_worker_role(repository, dispatch, hardened, role, decision):
    origin, repo = repository
    old = advance(origin, repo)
    card = dict(estimate_tokens=100, base_decision=decision)
    if role in ('review', 'judge'):
        (repo / 'review.txt').write_text('reviewed change')
        git(repo, 'add', 'review.txt')
        git(repo, 'commit', '-m', 'reviewed lane')
        card['diff_ref'] = dict(repo=str(repo), base=old, head=git(repo, 'rev-parse', 'HEAD'))
    service, calls = dispatch[0](hardened), dispatch[1]
    record = asyncio.run(service.spawn(card, 'Implement a tested change', role=role,
        writer_families=['deepseek'], dry_run=True, wait_seconds=None))
    assert record['status'] == ('dry_run' if decision == 'D-123' else 'failed')
    if decision != 'D-123':
        assert record['reason'] == 'base_stale: behind=2' and record['exit_code'] == 1
    else:
        assert record['base_sha'] == card['base_sha'] == git(origin, 'rev-parse', 'main')
        assert record['checked_base_sha'] == git(repo, 'rev-parse', 'HEAD')
    assert not calls
@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('mode', ['pinned', 'worktree', 'seats', 'seats_plain'])
def test_launch_variants_cannot_bypass(repository, dispatch, hardened, mode):
    from dataclasses import replace
    origin, repo = repository
    old = advance(origin, repo)
    service, calls = dispatch[0](hardened), dispatch[1]
    card, options = {'estimate_tokens': 100}, {}
    if mode == 'pinned':
        options = dict(leg='cx-gpt-6.1-sol-xhigh', override_reason='hermetic admission test')
    elif mode != 'seats_plain':
        git(repo, 'fetch', 'origin', 'main')
        git(repo, 'merge', '--ff-only', 'origin/main')
        git(repo, 'branch', 'lane/resume', old)
        card.update(lane_branch='lane/resume', continuation=True)
        options['run_worktree'] = True
    if mode in ('seats', 'seats_plain'):
        card['diff_ref'] = dict(repo=str(repo), base=old, head=old if mode == 'seats_plain' else git(repo, 'rev-parse', 'HEAD'))
        options.update(seats=True, role='review', writer_families=['deepseek'])
        route = service._route
        service._route = lambda *a, **k: replace(route(*a, **k), review_seats=route(*a, **k).legs)
    result = asyncio.run(service.spawn(card, 'Implement a tested change', wait_seconds=None, **options))
    for record in result.get('seats', [result]):
        assert record['status'] == 'failed' and record['reason'] == 'base_stale: behind=2'
        if mode in ('worktree', 'seats'):
            assert record['run_worktree']['base_sha'] == old and record['run_worktree']['removed']
    assert not calls
@pytest.mark.parametrize('role', ['implement', 'review', 'judge'])
@pytest.mark.parametrize('current', [False, True])
@pytest.mark.parametrize('diff', [True, False])
def test_continuation_rechecks_base(repository, dispatch, role, current, diff):
    origin, repo = repository
    old = advance(origin, repo)
    git(repo, 'fetch', 'origin', 'main')
    git(repo, 'merge', '--ff-only', 'origin/main')
    card = dict(estimate_tokens=100, **({'diff_ref': dict(repo=str(repo), base=old, head=git(repo, 'rev-parse', 'HEAD') if current else old)} if diff else {}))
    service, calls = dispatch[0](False), dispatch[1]
    parent = dict(run_id='run-parent', role=role, status='completed', exit_code=0,
        cwd=str(repo), card=card, chosen_leg=service._route().to_dict()['first_leg'])
    if not current:
        git(repo, 'switch', '--detach', old)
    asyncio.run(service._continue_writer(parent, 'Remaining gaps: finish the test'))
    if role != 'implement':
        assert parent['continuation_reason'] == 'not_writer' and 'continuation_run_id' not in parent
        assert not calls
        return
    child = service.result(parent['continuation_run_id'])
    assert child['status'] == ('completed' if current else 'failed'), child.get('error')
    assert len(calls) == int(current)
    if not current:
        assert child['reason'] == 'base_stale: behind=2'
def test_remove_refused_worktree_scrubs_git_env(repository, dispatch, monkeypatch):
    from plangraph.spawner.base_fresh import _remove_refused_worktree
    _, repo = repository
    path = repo.parent / 'worker'
    git(repo, 'worktree', 'add', '--detach', str(path), 'HEAD')
    for key in GIT_ENV:
        monkeypatch.setenv(key, str(repo / 'nonexistent'))
    record = {'run_worktree': {'path': str(path)}}
    _remove_refused_worktree(dispatch[0](False), record)
    assert record['run_worktree'].get('removed') and not path.exists()
@pytest.mark.parametrize('stale', [False, True])
def test_detached_cli_later_worktree_refusal_is_reported_by_status(repository, dispatch, monkeypatch, capsys, stale):
    from plangraph.spawner import cli
    origin, repo = repository
    old = advance(origin, repo)
    git(repo, 'fetch', 'origin', 'main')
    git(repo, 'merge', '--ff-only', 'origin/main')
    git(repo, 'branch', 'lane/resume', old if stale else 'HEAD')
    service = dispatch[0](True)
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    start = cli._start_supervisor
    script = f'''import asyncio
from plangraph.spawner import cli
from plangraph.spawner.runner import JobInfo
from plangraph.spawner.service import SpawnService
from plangraph.planner.routing import SpawnPlan
from plangraph.planner.legs import DEFAULT_LEGS
from types import SimpleNamespace
async def factory(**kwargs):
    await asyncio.sleep(3)
    return JobInfo('job-test', 'completed', kwargs['prompt'], kwargs['model_or_combo'], exit_code=0)
service = SpawnService(runner=SimpleNamespace(default_cwd={str(repo)!r}, spawn=factory, _tasks={{}}, _processes={{}}, provider_responses={{}}), state={str(service.state)!r})
service._route = lambda *a, **k: SpawnPlan((next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.1-sol-xhigh'),), (), 'test', (), ())
cli.SpawnService = lambda **k: service
raise SystemExit(cli.main(['--supervise']))
'''
    children = []
    def supervisor(*a, **kwargs):
        children.append(start([sys.executable, '-c', script], **kwargs))
        return children[-1]
    monkeypatch.setattr(cli, '_start_supervisor', supervisor)
    card = dict(estimate_tokens=100, lane_branch='lane/resume', continuation=True)
    started = time.monotonic()
    try:
        assert cli.main(['spawn', '--card', json.dumps(card), '--run-worktree', 'Implement a tested change']) == int(stale)
        assert time.monotonic() - started < 2.5
        record = json.loads(capsys.readouterr().out)
        assert record['status'] == ('failed' if stale else 'running')
        if stale:
            assert record['reason'] == 'base_stale: behind=2'
        else:
            assert record['base_admitted'] is True
    finally:
        for child in children:
            child.wait(timeout=10)
class _Out(io.StringIO):
    def close(self): pass
def test_supervisor_admission_timeout_fails_closed(dispatch, monkeypatch):
    from plangraph.spawner import cli, base_fresh
    service = dispatch[0](False)
    async def pending(**kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(service, 'spawn', pending)
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', .01)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(dict(state=str(service.state), card={}))))
    output = _Out()
    monkeypatch.setattr(cli.sys, 'stdout', output)
    assert cli.main(['--supervise']) == 1
    assert json.loads(output.getvalue())['reason'] == 'base_admission_timeout'
    assert not dispatch[1]
def test_admission_timeout_fails_original_run_records(dispatch, monkeypatch):
    from plangraph.spawner import cli, base_fresh, service as service_module
    service = dispatch[0](False)
    admit = service_module.admit_base
    async def hang(*args, launch=False, **kwargs):
        if not launch: return await admit(*args, **kwargs)
        await asyncio.Event().wait()
    monkeypatch.setattr(service_module, 'admit_base', hang)
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', .3)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(dict(state=str(service.state), card={'estimate_tokens': 100}, prompt='Implement a tested change'))))
    monkeypatch.setattr(cli.sys, 'stdout', _Out())
    assert cli.main(['--supervise']) == 1
    runs = service.ps()['runs']
    assert len(runs) > 1 and all(r['status'] != 'running' for r in runs)
def test_admission_wait_returns_refreshed_record_for_empty_seats(dispatch, monkeypatch):
    from plangraph.spawner.base_fresh import admitted_spawn
    service = dispatch[0](False)
    monkeypatch.setattr(service, 'spawn', lambda **kwargs: asyncio.sleep(0, dict(run_id='run-x', status='running', seats=[])))
    monkeypatch.setattr(service, 'result', lambda run_id: dict(run_id=run_id, status='failed', reason='base_stale: behind=1'))
    assert asyncio.run(admitted_spawn(service, {}))['status'] == 'failed'


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('diff', [False, True])
@pytest.mark.parametrize('launch', [False, True])
def test_review_checks_worker_checkout_even_with_fresh_diff(tmp_path, monkeypatch, role, diff, launch):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    calls, saved = [], []
    worker, reviewed = str(tmp_path / 'worker'), str(tmp_path / 'reviewed')
    def check(repo, default=None, fetch=True, timeout=60, ref='HEAD'):
        calls.append((repo, ref, fetch))
        stale = repo == worker and ref == 'HEAD'
        return dict(ok=not stale, reason='base_stale: behind=1' if stale else None,
                    origin_sha='b' * 40, base_sha='a' * 40, behind=int(stale))
    monkeypatch.setattr(base_fresh, '_check', check)
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=worker), _save=saved.append)
    card = {'diff_ref': dict(repo=reviewed, head='fresh-diff')} if diff else {}
    record = dict(role=role, cwd=worker)
    assert not asyncio.run(base_fresh.admit_base(service, record, card, launch=launch))
    assert record['reason'] == 'base_stale: behind=1'
    assert calls == [(worker, 'HEAD', True)] and saved == [record]


@pytest.mark.parametrize('supervisor', [False, True])
def test_split_writer_refusals_are_waited_and_reported(tmp_path, monkeypatch, capsys, supervisor):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from plangraph.spawner import cli, base_fresh
    runs = [dict(run_id='run-one', status='running'), dict(run_id='run-two', status='running')]
    final = {r['run_id']: dict(r, status='failed', exit_code=1, reason='base_stale: behind=1') for r in runs}
    service = SimpleNamespace(state=tmp_path, spawn=AsyncMock(return_value={'writers': runs}),
        result=lambda run_id: final[run_id], tasks={})
    if supervisor:
        monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
        monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps({'state': str(tmp_path)})))
        output = _Out()
        monkeypatch.setattr(cli.sys, 'stdout', output)
        assert cli.main(['--supervise']) == 1
        result = json.loads(output.getvalue())
    else:
        result = asyncio.run(base_fresh.admitted_spawn(service, {}))
    assert [r['status'] for r in result['writers']] == ['failed', 'failed']
    assert all(r['reason'] == 'base_stale: behind=1' for r in result['writers'])


def test_detached_cli_split_refusal_exits_nonzero(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from plangraph.spawner import cli
    refused = dict(writers=[dict(run_id='run-one', status='failed', exit_code=1, reason='base_stale: behind=1')])
    service = SimpleNamespace(state=tmp_path, route=lambda *a, **k: None)
    child = SimpleNamespace(stdin=_Out(), stdout=io.StringIO(json.dumps(refused) + '\n'))
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **k: child)
    assert cli.main(['spawn', '--card', '{"estimate_tokens":1}', 'Implement change']) != 0
    output = capsys.readouterr()
    assert json.loads(output.out) == refused
    assert 'base_stale: behind=1' in output.err


def test_admission_timeout_terminates_admitted_children_but_not_prior_runs(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from plangraph.spawner import base_fresh
    from plangraph.spawner.service import SpawnService
    runner = SimpleNamespace(jobs={'job-one': SimpleNamespace(survivors=[])}, cancel=AsyncMock())
    service = SpawnService(runner, state=tmp_path)
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', .02)
    async def hold():
        await asyncio.Event().wait()
    async def spawn(**kwargs):
        runs = [dict(run_id='run-one', status='running', base_admitted=True, job_id='job-one'),
                dict(run_id='run-two', status='running', base_admitted=False)]
        for run in runs:
            service._save(run)
            service.tasks[run['run_id']] = asyncio.create_task(hold())
        return {'writers': runs}
    monkeypatch.setattr(service, 'spawn', spawn)
    async def exercise():
        service._save(dict(run_id='run-prior', status='running'))
        prior = service.tasks['run-prior'] = asyncio.create_task(hold())
        try:
            result = await base_fresh.admitted_spawn(service, {})
            assert result['reason'] == 'base_admission_timeout'
            for name in ('run-one', 'run-two'):
                record = service.result(name)
                assert record['status'] == 'failed' and record['reason'] == 'base_admission_timeout'
                assert service.tasks[name].done()
            runner.cancel.assert_awaited_once_with('job-one')
            assert not prior.done() and service.result('run-prior')['status'] == 'running'
        finally:
            for task in service.tasks.values(): task.cancel()
            await asyncio.gather(*service.tasks.values(), return_exceptions=True)
    asyncio.run(exercise())


def test_admission_timeout_removes_unlaunched_worktree(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    from plangraph.spawner.service import SpawnService
    repo, attempt = tmp_path / 'repo', tmp_path / 'attempt'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'commit', '--allow-empty', '-m', 'initial')
    git(repo, 'worktree', 'add', '--detach', str(attempt), 'HEAD')
    service = SpawnService(SimpleNamespace(default_cwd=str(repo), jobs={}), state=tmp_path / 'state')
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', .02)
    async def hold():
        await asyncio.Event().wait()
    async def spawn(**kwargs):
        run = dict(run_id='run-pending', status='running', cwd=str(attempt), selected_cwd=str(repo),
                   base_admitted=False, run_worktree={'path': str(attempt)})
        service._save(run)
        service.tasks[run['run_id']] = asyncio.create_task(hold())
        return run
    monkeypatch.setattr(service, 'spawn', spawn)
    result = asyncio.run(base_fresh.admitted_spawn(service, {}))
    assert result['reason'] == 'base_admission_timeout'
    record = service.result('run-pending')
    assert not attempt.exists() and record['run_worktree']['removed']


def test_refused_continuation_cleanup_uses_selected_repository(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    selected = str(tmp_path / 'selected')
    attempt = tmp_path / 'attempt'
    record = dict(selected_cwd=selected, cwd=str(attempt), run_worktree={'path': str(attempt)})
    calls = []
    def remove(argv, **kwargs):
        calls.append(argv)
        if argv[2] != selected:
            raise subprocess.CalledProcessError(128, argv)
    monkeypatch.setattr(base_fresh.subprocess, 'run', remove)
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path / 'other')))
    base_fresh._remove_refused_worktree(service, record)
    assert record['run_worktree'].get('removed') is True
    assert calls[0][2] == selected


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('launch', [False, True])
@pytest.mark.parametrize('worker_fresh,diff_fresh', [(True, False), (False, True), (True, True)])
def test_review_requires_fresh_checkout_and_diff(tmp_path, monkeypatch, role, launch, worker_fresh, diff_fresh):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    worker, reviewed = str(tmp_path / 'worker'), str(tmp_path / 'reviewed')
    calls, saved = [], []
    def check(repo, default=None, fetch=True, timeout=60, ref='HEAD'):
        calls.append((repo, ref))
        fresh = worker_fresh if repo == worker else diff_fresh
        return dict(ok=fresh, reason=None if fresh else 'base_stale: behind=1',
                    origin_sha='b' * 40, base_sha=('b' if fresh else 'a') * 40, behind=int(not fresh))
    monkeypatch.setattr(base_fresh, '_check', check)
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=worker), _save=saved.append)
    card = dict(diff_ref=dict(repo=reviewed, head='reviewed-head'))
    record = dict(role=role, cwd=worker)
    assert asyncio.run(base_fresh.admit_base(service, record, card, launch=launch)) == (worker_fresh and diff_fresh)
    assert (worker, 'HEAD') in calls
    if worker_fresh:
        assert (reviewed, 'reviewed-head') in calls
    if worker_fresh and diff_fresh:
        assert record['checked_base_sha'] == 'b' * 40
    else:
        assert record['status'] == 'failed' and record['reason'] == 'base_stale: behind=1'
