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
from plangraph.spawner import leg_probe as _leg_probe
_REAL_BEFORE_LAUNCH = _leg_probe.before_launch  # the `dispatch` fixture replaces it
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
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'cx-gpt-6.2-sol-medium')
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
    assert record['status'] == 'failed'
    assert record['reason'] == 'base_stale: behind=2' and record['exit_code'] == 1
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
        options = dict(leg='cx-gpt-6.2-sol-medium', override_reason='hermetic admission test')
    elif mode != 'seats_plain':
        git(repo, 'fetch', 'origin', 'main')
        git(repo, 'merge', '--ff-only', 'origin/main')
        git(repo, 'branch', 'lane/resume', old)
        card.update(lane_branch='lane/resume', continuation=True)
        options['run_worktree'] = True
    if mode in ('seats', 'seats_plain'):
        # seats_plain reviews the stale head itself; its diff is the last commit, never empty.
        card['diff_ref'] = dict(repo=str(repo), base=old + '~1' if mode == 'seats_plain' else old,
                                head=old if mode == 'seats_plain' else git(repo, 'rev-parse', 'HEAD'))
        options.update(seats=True, role='review', writer_families=['deepseek'])
        route = service._route
        service._route = lambda *a, **k: replace(route(*a, **k), review_seats=route(*a, **k).legs)
    result = asyncio.run(service.spawn(card, 'Implement a tested change', wait_seconds=None, **options))
    for record in result.get('seats', [result]):
        if mode in ('worktree', 'seats'):
            assert record['status'] == 'refused'
            assert record['reason'] == 'base_stale: worktree_base_behind_origin'
            assert not record.get('run_worktree')
        else:
            assert record['status'] == 'failed' and record['reason'] == 'base_stale: behind=2'
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
    record = {'run_id': 'run-owned', 'run_worktree': {'path': str(path), 'created_by_run_id': 'run-owned'}}
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
    # Keep the harness running until the parent has checked that the CLI detached.
    release = repo.parent / 'finish-harness'
    script = f'''import asyncio
from pathlib import Path
from plangraph.spawner import cli, seat_spawn
seat_spawn.resources = lambda service: (0, 1000000)
from plangraph.spawner.runner import JobInfo
from plangraph.spawner.service import SpawnService
from plangraph.planner.routing import SpawnPlan
from plangraph.planner.legs import DEFAULT_LEGS
from types import SimpleNamespace
async def released():
    while not Path({str(release)!r}).exists():
        await asyncio.sleep(0.05)
async def factory(**kwargs):
    await asyncio.wait_for(released(), timeout=10)
    return JobInfo('job-test', 'completed', kwargs['prompt'], kwargs['model_or_combo'], exit_code=0)
service = SpawnService(runner=SimpleNamespace(default_cwd={str(repo)!r}, spawn=factory, _tasks={{}}, _processes={{}}, provider_responses={{}}), state={str(service.state)!r})
service._route = lambda *a, **k: SpawnPlan((next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.2-sol-medium'),), (), 'test', (), ())
cli.SpawnService = lambda **k: service
raise SystemExit(cli.main(['--supervise']))
'''
    children = []
    def supervisor(*a, **kwargs):
        children.append(start([sys.executable, '-c', script], **kwargs))
        return children[-1]
    monkeypatch.setattr(cli, '_start_supervisor', supervisor)
    card = dict(estimate_tokens=100, lane_branch='lane/resume', continuation=True)
    try:
        assert cli.main(['spawn', '--card', json.dumps(card), '--run-worktree', 'Implement a tested change']) == (2 if stale else 0)
        record = json.loads(capsys.readouterr().out)
        assert record['status'] == ('refused' if stale else 'running')
        if stale:
            assert record['reason'] == 'base_stale: worktree_base_behind_origin'
        else:
            assert record['base_admitted'] is True
            assert len(children) == 1 and children[0].poll() is None
    finally:
        release.touch()
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
@pytest.mark.parametrize('hardened', [False, True])
def test_admission_timeout_fails_original_run_records(dispatch, monkeypatch, hardened):
    from plangraph.spawner import cli, base_fresh, service as service_module, runner as runner_module
    service = dispatch[0](hardened)
    admit = service_module.admit_base
    async def hang(*args, launch=False, **kwargs):
        if not launch: return await admit(*args, **kwargs)
        await asyncio.Event().wait()
    monkeypatch.setattr(service_module, 'admit_base', hang)
    monkeypatch.setattr(runner_module, 'admit_base', hang)
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(base_fresh, 'ADMISSION_TIMEOUT', .3)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(dict(state=str(service.state), card={'estimate_tokens': 100}, prompt='Implement a tested change'))))
    monkeypatch.setattr(cli.sys, 'stdout', _Out())
    assert cli.main(['--supervise']) == 1
    runs = service.ps()['runs']
    assert len(runs) > 1
    assert all(r['status'] == 'failed' and r['reason'] == 'base_admission_timeout' for r in runs)
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
                   base_admitted=False, run_worktree={'path': str(attempt), 'created_by_run_id': 'run-pending'})
        service._save(run)
        service.tasks[run['run_id']] = asyncio.create_task(hold())
        return run
    monkeypatch.setattr(service, 'spawn', spawn)
    result = asyncio.run(base_fresh.admitted_spawn(service, {}))
    assert result['reason'] == 'base_admission_timeout'
    record = service.result('run-pending')
    assert not attempt.exists() and record['run_worktree']['removed']


def test_refused_owned_attempt_cleanup_uses_selected_repository(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    selected = str(tmp_path / 'selected')
    attempt = tmp_path / 'attempt'
    record = dict(run_id='run-owned', selected_cwd=selected, cwd=str(attempt),
                  run_worktree={'path': str(attempt), 'created_by_run_id': 'run-owned'})
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
        calls.append((repo, ref, fetch))
        fresh = worker_fresh if repo == worker else diff_fresh
        return dict(ok=fresh, reason=None if fresh else 'base_stale: behind=1',
                    origin_sha='b' * 40, base_sha=('b' if fresh else 'a') * 40, behind=int(not fresh))
    monkeypatch.setattr(base_fresh, '_check', check)
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=worker), _save=saved.append)
    card = dict(diff_ref=dict(repo=reviewed, head='reviewed-head'))
    record = dict(role=role, cwd=worker)
    assert asyncio.run(base_fresh.admit_base(service, record, card, launch=launch)) == (worker_fresh and diff_fresh)
    assert (worker, 'HEAD', True) in calls
    if worker_fresh:
        assert (reviewed, 'reviewed-head', True) in calls
    if worker_fresh and diff_fresh:
        assert record['checked_base_sha'] == 'b' * 40
    else:
        assert record['status'] == 'failed' and record['reason'] == 'base_stale: behind=1'


def test_worktree_gate_fast_forwards_clean_main(repository, tmp_path):
    from plangraph.spawner.base_fresh import fresh_worktree_base
    origin, repo = repository
    old = advance(origin, repo)
    expected = git(origin, 'rev-parse', 'main')
    assert git(repo, 'rev-parse', 'HEAD') == old
    assert fresh_worktree_base(repo) == expected
    assert git(repo, 'rev-parse', 'HEAD') == expected


@pytest.mark.parametrize('damage', ['dirty', 'stale', 'diverged'])
def test_worktree_gate_refuses_before_creation(repository, damage):
    from plangraph.spawner.base_fresh import fresh_worktree_base
    from plangraph.routing_errors import SpawnRefused
    origin, repo = repository
    old = advance(origin, repo)
    if damage == 'dirty':
        (repo / 'untracked').write_text('keep')
    elif damage == 'diverged':
        git(repo, 'commit', '--allow-empty', '-m', 'local divergence')
    with pytest.raises(SpawnRefused, match='base_stale'):
        fresh_worktree_base(repo, base=old if damage == 'stale' else 'origin/main')
    assert git(repo, 'worktree', 'list', '--porcelain').count('worktree ') == 1


def test_phase_worktree_gate_checks_phase_tracking_head(repository):
    from plangraph.spawner.base_fresh import fresh_worktree_base
    from plangraph.routing_errors import SpawnRefused
    _, repo = repository
    git(repo, 'checkout', '-b', 'phase/test')
    old = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'commit', '--allow-empty', '-m', 'phase')
    git(repo, 'push', 'origin', 'HEAD:phase/test')
    git(repo, 'reset', '--hard', old)
    with pytest.raises(SpawnRefused, match='base_stale'):
        fresh_worktree_base(repo, base='phase/test', default='phase/test')
    assert fresh_worktree_base(repo, base='origin/phase/test', default='phase/test') != old


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('launch', [False, True])
@pytest.mark.parametrize('offline', [False, True])
def test_separate_review_repository_fetches_before_admission(repository, role, launch, offline):
    from types import SimpleNamespace
    from plangraph.spawner.base_fresh import admit_base
    origin, worker = repository
    reviewed = worker.parent / 'reviewed'
    git(worker.parent, 'clone', str(origin), str(reviewed))
    old = git(reviewed, 'rev-parse', 'HEAD')
    advance(origin, worker)
    git(worker, 'fetch', 'origin', 'main')
    git(worker, 'merge', '--ff-only', 'origin/main')
    assert git(reviewed, 'rev-parse', 'origin/main') == old
    if offline:
        git(reviewed, 'remote', 'set-url', 'origin', str(worker.parent / 'missing-origin'))
    saved = []
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(worker)), _save=saved.append)
    record = dict(role=role, cwd=str(worker))
    card = dict(diff_ref=dict(repo=str(reviewed), head=old))
    assert not asyncio.run(admit_base(service, record, card, launch=launch))
    assert record['reason'] == ('base_stale: fetch_failed' if offline else 'base_stale: behind=2')
    assert git(reviewed, 'rev-parse', 'origin/main') == (old if offline else git(origin, 'rev-parse', 'main'))
    assert saved == [record]


@pytest.mark.parametrize('alias', [False, True])
def test_same_review_repository_fetches_only_once(tmp_path, monkeypatch, alias):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    worker = str(tmp_path / 'worker')
    reviewed = str(tmp_path / 'worker/../worker') if alias else worker
    calls = []
    def check(repo, default=None, fetch=True, timeout=60, ref='HEAD'):
        calls.append(fetch)
        return dict(ok=True, origin_sha='b' * 40, base_sha='b' * 40)
    monkeypatch.setattr(base_fresh, '_check', check)
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=worker), _save=lambda r: None)
    assert asyncio.run(base_fresh.admit_base(service, dict(role='review', cwd=worker),
        dict(diff_ref=dict(repo=reviewed, head='HEAD'))))
    assert calls == [True, False]


@pytest.mark.parametrize('continuation,owned', [(True, True), (False, False)])
def test_split_cleanup_preserves_unowned_or_continuation_tree(tmp_path, monkeypatch, continuation, owned):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    calls = []
    monkeypatch.setattr(base_fresh.subprocess, 'run', lambda *a, **k: calls.append(a))
    record = dict(run_id='run-child', cwd=str(tmp_path), card={'continuation': continuation},
                  run_worktree=dict(path=str(tmp_path / 'evidence'), continuation=continuation,
                                    created_by_run_id='run-child' if owned else 'run-parent'))
    base_fresh._remove_refused_worktree(SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path))), record)
    assert not calls


@pytest.mark.parametrize('launch', [False, True])
def test_split_fabricated_decision_refuses_stale_base(tmp_path, monkeypatch, launch):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    monkeypatch.delenv('PLANGRAPH_BASE_DECISIONS_FILE', raising=False)
    monkeypatch.setattr(base_fresh, 'base_fresh', lambda *a, **k: dict(ok=False,
        reason='base_stale: behind=2', behind=2, origin_sha='b'*40, base_sha='a'*40))
    record = dict(role='implement', cwd=str(tmp_path))
    assert not asyncio.run(base_fresh.admit_base(SimpleNamespace(_save=lambda r: None), record,
        {'base_decision': 'D-123'}, launch=launch))


@pytest.mark.parametrize('launch', [False, True])
@pytest.mark.parametrize('source', ['operator', 'judge'])
@pytest.mark.parametrize('offline', [False, True])
def test_split_waiver_uses_authoritative_operator_source(tmp_path, monkeypatch, launch, source, offline):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    from plangraph.keeper import evidence_decisions
    base, origin = 'a'*40, 'b'*40
    path = tmp_path / 'decisions.md'
    path.write_text('| D-123 | date | Source: operator Waives: spawn-base:' + base + ' Origin: ' + origin + ' |\n')
    monkeypatch.setenv('PLANGRAPH_BASE_DECISIONS_FILE', str(path))
    seen = []
    def fetched(configured):
        seen.append(configured)
        if offline:
            raise OSError('offline')
        return '| D-123 | date | Source: ' + source + ' Waives: spawn-base:' + base + ' Origin: ' + origin + ' |\n'
    monkeypatch.setattr(evidence_decisions, 'read_decisions_origin', fetched)
    monkeypatch.setattr(base_fresh, 'base_fresh', lambda *a, **k: dict(ok=False,
        reason='base_stale: behind=2', behind=2, origin_sha=origin, base_sha=base))
    record = dict(role='implement', cwd=str(tmp_path))
    admitted = asyncio.run(base_fresh.admit_base(SimpleNamespace(_save=lambda r: None), record,
        {'base_decision': 'D-123'}, launch=launch))
    assert admitted == (source == 'operator' and not offline)
    assert seen == [path]


def test_split_waiver_never_overrides_worker_fetch_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh
    monkeypatch.setattr(base_fresh, 'base_fresh', lambda *a, **k: dict(ok=False,
        reason='base_stale: fetch_failed', behind=None, origin_sha=None, base_sha=None))
    monkeypatch.setattr(base_fresh, '_decision_waives', lambda *a: pytest.fail('fetch failure cannot be waived'))
    assert not asyncio.run(base_fresh.admit_base(SimpleNamespace(_save=lambda r: None),
        dict(role='implement', cwd=str(tmp_path)), {'base_decision': 'D-123'}, launch=True))


def test_base_fresh_checks_the_named_ref_not_head(repository):
    from plangraph.spawner.base_fresh import base_fresh
    origin, repo = repository
    old = advance(origin, repo)
    assert base_fresh(repo)['ok'] is False
    assert base_fresh(repo, ref=old)['reason'] == 'base_stale: behind=2'
    git(repo, 'fetch', '-q', 'origin')
    git(repo, 'merge', '--ff-only', 'origin/main')
    current = git(repo, 'rev-parse', 'HEAD')
    assert base_fresh(repo, ref=current, fetch=False)['ok'] is True
    assert base_fresh(repo, ref=old, fetch=False)['ok'] is False


def lane_clone(repository, tmp_path):
    """A clone of a lane-branch checkout, its origin pointed back at the real remote."""
    origin, repo = repository
    git(repo, 'checkout', '-b', 'lane/x')
    git(repo, 'commit', '--allow-empty', '-m', 'lane work')
    clone = tmp_path / 'clone'
    git(tmp_path, 'clone', '-q', str(repo), str(clone))
    git(clone, 'remote', 'set-url', 'origin', str(origin))
    assert git(clone, 'symbolic-ref', 'refs/remotes/origin/HEAD') == 'refs/remotes/origin/lane/x'
    return origin, clone


def test_origin_head_naming_an_absent_remote_branch_is_unknown(repository, tmp_path):
    from plangraph.spawner.base_fresh import base_fresh
    origin, clone = lane_clone(repository, tmp_path)
    result = base_fresh(clone)
    assert result['ok'] is True and result['reason'] is None
    assert result['default_branch'] == 'main'
    assert result['origin_sha'] == git(origin, 'rev-parse', 'main')


def test_origin_head_naming_an_absent_remote_branch_still_refuses_a_stale_head(repository, tmp_path):
    from plangraph.spawner.base_fresh import base_fresh
    origin, repo = repository
    old = advance(origin, repo)
    _, clone = lane_clone(repository, tmp_path)
    git(clone, 'checkout', '-q', '--detach', old)
    result = base_fresh(clone)
    assert result['ok'] is False and result['reason'] == 'base_stale: behind=2'
    assert result['default_branch'] == 'main'


def test_origin_head_naming_a_present_remote_branch_stays_the_default(repository, tmp_path):
    from plangraph.spawner.base_fresh import base_fresh
    origin, repo = repository
    git(repo, 'push', 'origin', 'HEAD:refs/heads/trunk')
    git(origin, 'symbolic-ref', 'HEAD', 'refs/heads/trunk')
    clone = tmp_path / 'trunk-clone'
    git(tmp_path, 'clone', '-q', str(origin), str(clone))
    assert git(clone, 'symbolic-ref', 'refs/remotes/origin/HEAD') == 'refs/remotes/origin/trunk'
    result = base_fresh(clone)
    assert result['default_branch'] == 'trunk' and result['ok'] is True


def test_same_checkout_review_reuses_the_resolved_default_branch(repository, tmp_path):
    """A lane-checkout clone passes both admission checks: the cached one reuses main."""
    from types import SimpleNamespace
    from plangraph.spawner.base_fresh import admit_base
    _, clone = lane_clone(repository, tmp_path)
    # Review the main tip while origin/HEAD stays on the lane: only the resolved default passes.
    head = git(clone, 'rev-parse', 'origin/main')
    assert head != git(clone, 'rev-parse', 'origin/lane/x')
    record = dict(role='review', cwd=str(clone))
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(clone)), _save=lambda r: None)
    card = dict(diff_ref=dict(repo=str(clone), base='HEAD', head=head))
    assert asyncio.run(admit_base(service, record, card, launch=True)) is True
    assert record['base_sha'] == git(clone, 'rev-parse', 'origin/main')


def test_stuck_initiating_probe_is_a_leg_skip_not_an_admission_timeout(repository, dispatch, monkeypatch):
    """A probe stuck in its own setup fails the leg inside the admission window."""
    from functools import partial
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.setattr(leg_probe, 'before_launch', _REAL_BEFORE_LAUNCH)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'ProbeBudget', partial(leg_probe.ProbeBudget, window=1.8))
    now = 100.0
    clock = SimpleNamespace(monotonic=lambda: now, time=lambda: 0.0, sleep=time.sleep)
    monkeypatch.setattr(base_fresh, 'time', clock)
    monkeypatch.setattr(leg_probe, 'time', clock)
    probe_timeouts = []

    async def stuck(runner, leg, *, timeout_s, **kwargs):
        nonlocal now
        probe_timeouts.append(timeout_s)
        now += 0.7  # The owner uses its 0.6 s share and 0.1 s of grace.
        await asyncio.Future()  # Stuck before the probe can enforce its own deadline.

    monkeypatch.setattr(leg_probe, 'probe', stuck)
    # The hardened service runs the leg loop that probes.
    service, calls = dispatch[0](True), dispatch[1]
    async def main():
        record = await base_fresh.admitted_spawn(service, dict(
            card=dict(estimate_tokens=100), prompt='Implement a tested change', role='implement'))
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
        return record
    record = asyncio.run(main())
    assert probe_timeouts == pytest.approx([0.6])
    assert 'base_admission_timeout' not in json.dumps(record), record
    attempts = service.result(record['run_id'])['attempts']
    assert [a['skip_reason'] for a in attempts] == ['probe_budget_exhausted'], attempts
    assert not calls


def test_probe_budget_counts_routing_time_spent_before_the_run_record(repository, dispatch, monkeypatch):
    """Routing before run record creation consumes the probe budget."""
    from functools import partial
    from types import SimpleNamespace
    from plangraph.spawner import base_fresh, leg_probe
    monkeypatch.setattr(leg_probe, 'before_launch', _REAL_BEFORE_LAUNCH)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    # Shorten the synthetic probe window while leaving the real admission watchdog alone.
    monkeypatch.setattr(leg_probe, 'ProbeBudget', partial(leg_probe.ProbeBudget, window=1.8))
    now = 100.0
    clock = SimpleNamespace(monotonic=lambda: now, time=lambda: 0.0)
    monkeypatch.setattr(base_fresh, 'time', clock)
    monkeypatch.setattr(leg_probe, 'time', clock)

    async def unexpected_probe(*args, **kwargs):
        pytest.fail('routing already exhausted the probe budget')

    monkeypatch.setattr(leg_probe, 'probe', unexpected_probe)
    service, calls = dispatch[0](True), dispatch[1]

    async def slow_plan(plan, **kwargs):
        nonlocal now
        now += 1.9  # Before the run record exists.
        return plan

    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', slow_plan)

    async def main():
        record = await base_fresh.admitted_spawn(service, dict(
            card=dict(estimate_tokens=100), prompt='Implement a tested change', role='implement'))
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
        return record

    record = asyncio.run(main())
    assert 'base_admission_timeout' not in json.dumps(record), record
    attempts = service.result(record['run_id'])['attempts']
    assert [a['skip_reason'] for a in attempts] == ['probe_budget_exhausted'], attempts
    assert not calls


def test_admitted_spawn_leaves_the_callers_admission_clock_unset(repository, dispatch):
    from plangraph.spawner import base_fresh
    service = dispatch[0](True)
    async def main():
        await base_fresh.admitted_spawn(service, dict(
            card=dict(estimate_tokens=100), prompt='Implement a tested change', role='implement'))
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
        return base_fresh.ADMISSION_STARTED.get()
    assert asyncio.run(main()) is None


def _wall_clock_bounds(source, path):
    import ast
    import operator
    from collections import defaultdict

    arithmetic = {ast.Add: operator.add, ast.Sub: operator.sub,
                  ast.Mult: operator.mul, ast.Div: operator.truediv}

    def constant_seconds(expr):
        if isinstance(expr, ast.Constant) and type(expr.value) in (float, int):
            return expr.value
        if isinstance(expr, ast.UnaryOp):
            operation = {ast.UAdd: operator.pos, ast.USub: operator.neg}.get(type(expr.op))
            if operation and (operand := constant_seconds(expr.operand)) is not None:
                return operation(operand)
        if isinstance(expr, ast.BinOp) and (operation := arithmetic.get(type(expr.op))):
            left, right = constant_seconds(expr.left), constant_seconds(expr.right)
            if left is not None and right is not None:
                try:
                    return operation(left, right)
                except ArithmeticError:
                    return None
        return None

    def expression_name(expr):
        if isinstance(expr, ast.Name):
            return expr.id
        if isinstance(expr, ast.Attribute):
            return expr.attr
        if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant):
            return expr.slice.value
        return None

    def measures_elapsed(expr):
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Sub):
            # Timestamp differences can start with a recorded sample rather than a clock call.
            if (isinstance(expr.left, (ast.Name, ast.Attribute, ast.Subscript))
                    and expression_name(expr.right) in {'start', 'started', 'start_time', 't0'}):
                return True
            expr = expr.left
        if isinstance(expr, ast.Call):
            return expression_name(expr.func) in {'monotonic', 'perf_counter', 'time'}
        return expression_name(expr) in {'elapsed', 'waited', 'duration', 'elapsed_s', 'duration_s'}

    def is_short_bound(comparison):
        operands = [comparison.left, *comparison.comparators]
        for left, op, right in zip(operands, comparison.ops, operands[1:]):
            if isinstance(op, (ast.Gt, ast.GtE)):
                left, right = right, left
            elif not isinstance(op, (ast.Lt, ast.LtE)):
                continue
            seconds = constant_seconds(right)
            if seconds is not None and 0 < seconds < 5 and measures_elapsed(left):
                return True
        return False

    class FindBounds(ast.NodeVisitor):
        def __init__(self, path):
            self.path = path
            self.scope = []

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

        def visit_Assert(self, node):
            for expr in ast.walk(node.test):
                if isinstance(expr, ast.Compare) and is_short_bound(expr):
                    depth = next((i + 1 for i, part in enumerate(self.scope)
                                  if part.startswith('test_')), len(self.scope))
                    name = '::'.join(self.scope[:depth]) or '<module>'
                    matches[f'{self.path}::{name}'].append(expr.lineno)

    matches = defaultdict(list)
    FindBounds(path).visit(ast.parse(source, filename=path))
    return matches


@pytest.mark.parametrize('bound, matched', [
    ('end - start < 2.5', True),
    ('fake.builds[0] - t0 < 0.5 + 0.3', True),
    ('0.5 + 0.3 > fake.builds[0] - t0', True),
    ('total - count < 2.5', False),
    ('end - start < 4 + 1', False),
])
def test_wall_clock_ratchet_detects_subtracted_timestamps(bound, matched):
    source = f'def test_example():\n    assert {bound}\n'
    expected = {'tests/example.py::test_example': [2]} if matched else {}
    assert _wall_clock_bounds(source, 'tests/example.py') == expected


def test_no_sub_five_second_wall_clock_assertions():
    """Ratchet short upper bounds on elapsed time in Python tests."""
    from pathlib import Path

    tests = Path(__file__).resolve().parent
    matches = {}
    for path in sorted(tests.rglob('*.py')):
        relative = path.relative_to(tests.parent).as_posix()
        matches.update(_wall_clock_bounds(path.read_bytes(), relative))

    # Each entry is a known flake risk to convert to a deterministic assertion.
    allowed = set('''\
tests/hooks/test_redact_common.py::test_scans_stay_linear_on_hostile_lines
tests/test_devstack_sh.py::test_dying_start_dev_child_fails_fast
tests/test_e2e_teardown_reap.py::test_term_ignorer
tests/test_fleet_msg.py::test_prober_answers_unreachable_peers_within_budget
tests/test_jev_attempt_thread.py::test_hanging_jev_does_not_delay_production_decisions
tests/test_jev_client.py::test_timeout_is_recorded_and_bounded
tests/test_keeper_bus_fatal.py::test_fatal_before_on_fatal_wiring_exits_75
tests/test_keeper_ready_scale.py::test_cold_read_builds_in_the_request_expired_read_serves_the_old_index
tests/test_keeper_review_jev_shadow.py::test_a_failing_jev_never_changes_the_verdict_result
tests/test_keeper_seat_clause_tier.py::test_adversarial_length
tests/test_keeper_snapshot_writer.py::test_a_steady_stream_cannot_starve_the_file
tests/test_keeper_snapshot_writer.py::test_show_answers_during_a_fleet_build_of_7000_records
tests/test_kg_export.py::test_slow_drip_response_hits_total_budget
tests/test_m2b_anchor.py::test_silent_service_times_out_quickly
tests/test_planner_jev_shadow.py::test_guarded_ok_timeout_and_error
tests/test_planner_jev_shadow.py::test_timeout_is_bounded_and_reported_for_every_kind
tests/test_prod_audit_detached.py::test_stalled_log_runtime_count_is_capped
tests/test_prod_audit_safety.py::test_interpreter_startup_is_inside_deadline
tests/test_prod_audit_safety.py::test_stalled_audit_is_bounded
tests/test_redact_output_rules.py::test_unterminated_pem_scan_is_linear
tests/test_review_jev_hook.py::test_a_bad_jev_never_blocks_plan_actions_or_changes_the_plan
tests/test_spawn_cli.py::test_cli_wait_returns_after_the_dispatcher_died
tests/test_spawner_quota_failfast.py::test_call_log_poll_is_time_bounded
tests/test_spawner_remote_ps.py::test_wsl_query_deadline_holds_when_the_reader_never_drains_the_plan
'''.splitlines())
    unexpected = [f'{name} at lines {lines}' for name, lines in matches.items()
                  if name not in allowed or len(lines) > 1]
    stale = sorted(allowed - matches.keys())
    assert not (unexpected or stale), (
        f'Unexpected sub-five-second wall-clock assertions: {unexpected}\n'
        f'Stale wall-clock allowlist entries: {stale}'
    )


from unittest.mock import Mock

REASON = 'cargo_target_cap: measured 68.42 GB in /x, cap 64 GB; busy'


def _supervise_with(monkeypatch, tmp_path, service, admitted):
    from plangraph.spawner import cli
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(cli, 'admitted_spawn', admitted)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps({'state': str(tmp_path)})))
    out = _Out()
    monkeypatch.setattr(cli.sys, 'stdout', out)
    return cli.main(['--supervise']), out


def test_supervisor_reports_a_refusal_after_admission_as_refused(tmp_path, monkeypatch, capsys):
    """A cap refusal at launch follows the 'running' admission snapshot; it is not EXIT 0."""
    from types import SimpleNamespace
    final = dict(run_id='run-one', status='refused', exit_code=2, reason=REASON, ended=1.0)

    async def admitted(service, payload):
        async def launch():
            return None
        service.tasks = {'run-one': asyncio.ensure_future(launch())}
        return dict(run_id='run-one', status='running')
    service = SimpleNamespace(state=tmp_path, result=lambda run_id: final, tasks={})
    code, _ = _supervise_with(monkeypatch, tmp_path, service, admitted)
    assert code == 2
    assert f'ALERT SPAWN run-one REFUSED: {REASON}' in capsys.readouterr().err


def test_supervisor_alerts_on_refusals_that_never_had_a_task(tmp_path, monkeypatch, capsys):
    """Taskless refusals alert from the saved record; two refusals with one error text stay apart."""
    from plangraph.spawner import cli
    from plangraph.planner.routing import SpawnRefused
    sink, keeper = Mock(), Mock()
    service = cli.SpawnService(state=tmp_path, completion_sink=sink, keeper_sink=keeper)
    other = service.save_refusal(SpawnRefused(REASON), 'implement')
    saved = service.save_refusal(SpawnRefused(REASON), 'implement')
    assert other['error'] == saved['error'] and other['run_id'] != saved['run_id']
    delivered = sink.deliver.call_count

    async def returned(service, payload):
        return dict(saved, exit_code=0)   # a stale snapshot: the saved record decides
    code, _ = _supervise_with(monkeypatch, tmp_path, service, returned)
    err = capsys.readouterr().err
    assert code == 2 and f"ALERT SPAWN {saved['run_id']} REFUSED: {REASON}" in err

    async def raising(service, payload):
        exc = SpawnRefused(REASON)
        exc.run_id = saved['run_id']   # what SeatSpawnMixin.spawn attaches after save_refusal
        raise exc
    code, out = _supervise_with(monkeypatch, tmp_path, service, raising)
    printed = json.loads(out.getvalue())
    assert code == 2 and printed['run_id'] == saved['run_id']
    assert f"ALERT SPAWN {saved['run_id']} REFUSED" in capsys.readouterr().err
    assert len(service.ps()['runs']) == 2   # no third refusal was minted
    assert sink.deliver.call_count == delivered   # reads never deliver again

    async def lost(service, payload):
        exc = SpawnRefused(REASON)
        exc.run_id = 'run-' + 'f' * 32   # the record never reached the disk
        raise exc
    code, out = _supervise_with(monkeypatch, tmp_path, service, lost)
    printed = json.loads(out.getvalue())
    assert code != 0 and printed['run_id'] == 'run-' + 'f' * 32 and printed['status'] == 'failed'
    assert len(service.ps()['runs']) == 2


def test_spawn_attaches_the_saved_refusal_run_id_to_the_exception(tmp_path, monkeypatch):
    from plangraph.spawner import cli
    from plangraph.planner.routing import SpawnRefused
    service = cli.SpawnService(state=tmp_path)

    async def refuse(*args, **kwargs):
        raise SpawnRefused(REASON)
    monkeypatch.setattr(service, '_spawn', refuse)
    with pytest.raises(SpawnRefused) as raised:
        asyncio.run(service.spawn(card={'estimate_tokens': 1}, prompt='x', role='implement', wait_seconds=0))
    assert service.result(raised.value.run_id)['status'] == 'refused'


def _native_cap_refusal(tmp_path, monkeypatch, state_name='state'):
    """The real native cargo-cap admission refusal: no task and no run directory work happened."""
    from plangraph.spawner import host_ready, overflow, seat_spawn
    keeper, parent = Mock(), Mock()
    service = overflow.cli.SpawnService(state=tmp_path / state_name, keeper_sink=keeper, completion_sink=parent)
    monkeypatch.setenv('PLANGRAPH_ADMISSION_WAIT_S', '0')
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 50000))
    monkeypatch.setattr(overflow, 'placement_policy', lambda *args: ('fixture', False))
    monkeypatch.setattr(overflow, 'placement_config', lambda *args: {'hosts': {}})
    monkeypatch.setattr(overflow, 'ready_records', lambda *args: {})
    monkeypatch.setattr(host_ready, 'local_disk_admission', lambda *args, **kwargs: (None, REASON))
    card = dict(run_kind='native-codex', repo=str(tmp_path), item_key='demo:A', parent_session='fixture-parent')
    return service, service.admit_spawn({'role': 'implement'}, card=card), keeper, parent


@pytest.mark.parametrize('saved_code', ['absent', 0, None, '2', 2])
def test_native_spawn_wait_reports_persisted_cargo_cap_refusal(tmp_path, monkeypatch, capsys, saved_code):
    from plangraph.spawner import cli
    service, refusal, keeper, parent = _native_cap_refusal(tmp_path, monkeypatch)
    assert refusal['status'] == 'refused' and refusal['run_id'] not in service.tasks
    if saved_code != 'absent':
        refusal['exit_code'] = saved_code
    else:
        refusal.pop('exit_code', None)
    service._save(refusal)
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    for _ in range(2):   # repeated reads never deliver again
        assert cli.main(['--state', str(tmp_path / 'state'), 'wait', refusal['run_id']]) not in (0, None)
    out = capsys.readouterr().out
    assert REASON in out
    assert keeper.deliver.call_count == 1 and parent.deliver.call_count == 1
    # The origin host's receipt of the same run is a forwarded read: it sends nothing of its own.
    origin_keeper, origin_parent = Mock(), Mock()
    from plangraph.spawner.runner import spawn_service
    origin = spawn_service(state=tmp_path / 'origin', keeper_sink=origin_keeper, completion_sink=origin_parent)
    origin._save(dict(service.result(refusal['run_id']), overflow_remote=True, host='target-host'))
    assert origin.result(refusal['run_id'])['origin_receipt'] is True
    origin.retry_completions()
    assert origin_keeper.deliver.call_count == 0 and origin_parent.deliver.call_count == 0


class _Supervisor:
    """A supervisor double that acknowledges the admission and ends the run later."""
    def __init__(self, service, ack, steps):
        import threading
        self.stdin, self.stdout = io.StringIO(), io.StringIO(json.dumps(ack) + '\n')
        service._save(dict(ack, started=time.time(), ended=None))
        def play():
            for delay, change in steps:
                time.sleep(delay)
                service._save(dict(service.result(ack['run_id']), **change))
        self.thread = threading.Thread(target=play, daemon=True)
        self.thread.start()
    def wait(self): return 0


@pytest.mark.parametrize('steps,code,status', [
    ([(.05, dict(status='completed', exit_code=0, ended=1.0))], 0, 'completed'),
    ([(.05, dict(status='failed', exit_code=0, ended=1.0))], 1, 'failed'),          # a saved failure when the worker exits zero
    ([(.05, dict(status='failed', exit_code=3, ended=1.0))], 3, 'failed'),
    ([(.05, dict(status='refused', exit_code=0, ended=1.0, reason=REASON))], 1, 'refused'),
    ([(.05, dict(status='stopped', exit_code=130, ended=1.0))], 130, 'stopped'),
    ([(.05, dict(status='cancelled', exit_code=None, ended=1.0))], 1, 'cancelled'),
    # status before `ended`: the first poll must not reply from the provisional status
    ([(.05, dict(status='refused', exit_code=2, reason=REASON)), (.3, dict(ended=1.0))], 2, 'refused'),
])
def test_native_spawn_wait_preserves_failed_and_completed_status(tmp_path, monkeypatch, capsys, steps, code, status):
    from plangraph.spawner import cli
    service = cli.SpawnService(state=tmp_path, completion_sink=Mock(), keeper_sink=Mock())
    ack = dict(run_id='run-' + 'a' * 32, run_kind='native-codex', role='implement', status='running')
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **k: _Supervisor(service, ack, steps))
    card = json.dumps(dict(run_kind='native-codex', repo=str(tmp_path), estimate_tokens=1))
    assert cli.main(['--state', str(tmp_path), 'spawn', '--card', card, '--wait', '5', 'prompt']) == code
    final = json.loads(capsys.readouterr().out)
    assert final['status'] == status and final['ended'] is not None   # settled, never the provisional read
    assert cli.main(['--state', str(tmp_path), 'wait', ack['run_id']]) == code   # one rule on both paths


def test_wait_runs_does_not_reply_from_a_status_before_ended(tmp_path):
    from plangraph.spawner import cli
    service = cli.SpawnService(state=tmp_path, completion_sink=Mock(), keeper_sink=Mock())
    run_id = 'run-' + 'b' * 32
    service._save(dict(run_id=run_id, status='refused', exit_code=2, reason=REASON, started=time.time(), ended=None))
    result = dict(run_id=run_id, status='running')
    cli._wait_runs(service, result, .1)
    assert result.get('ended') is None   # still unsettled when the bound ended: not reported as final
    service._save(dict(service.result(run_id), ended=1.0))
    cli._wait_runs(service, result, 2)
    assert result['ended'] == 1.0


def test_a_forwarded_receipt_without_ended_keeps_refreshing():
    from plangraph.spawner.service import remote_native_running
    receipt = dict(run_kind='native-codex', overflow_remote=True, status='refused', exit_code=2, ended=None)
    assert remote_native_running(receipt)
    assert not remote_native_running(dict(receipt, ended=1.0))
    assert not remote_native_running(dict(receipt, status='queued'))


def test_mcp_native_spawn_with_a_positive_wait_returns_the_settled_refusal(tmp_path, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path)
    service = server.service
    run_id = 'run-' + 'c' * 32
    ack = dict(run_id=run_id, run_kind='native-codex', role='implement', status='running', started=time.time())

    async def spawn(**kwargs):
        service._save(dict(ack))
        def finish():
            time.sleep(.1)
            service._save(dict(ack, status='refused', exit_code=2, reason=REASON, ended=time.time()))
        import threading
        threading.Thread(target=finish, daemon=True).start()
        return dict(ack)
    monkeypatch.setattr(service, 'spawn', spawn)
    card = dict(run_kind='native-codex', repo=str(tmp_path))
    settled = asyncio.run(server.call_tool('plangraph_agent_spawn', dict(card=card, prompt='p', wait_seconds=5)))
    assert settled['run_id'] == run_id and settled['status'] == 'refused' and settled['ended'] is not None
    ack_only = asyncio.run(server.call_tool('plangraph_agent_spawn', dict(card=card, prompt='p')))
    assert ack_only['status'] == 'running'   # no wait requested: the admission acknowledgement


def test_spawn_wait_timeout_does_not_report_a_provisional_native_refusal(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import cli
    service = cli.SpawnService(state=tmp_path, completion_sink=Mock(), keeper_sink=Mock())
    run_id = 'run-' + 'd' * 32
    pending = dict(run_id=run_id, run_kind='native-codex', role='implement',
                   status='refused', reason=REASON, exit_code=8)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: service)
    monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **kw: _Supervisor(service, pending, []))
    card = json.dumps(dict(run_kind='native-codex', repo=str(tmp_path), estimate_tokens=1))

    assert cli.main(['--state', str(tmp_path), 'spawn', '--card', card, '--wait', '.04', 'prompt']) == 1
    result = capsys.readouterr()
    assert result.out == '' and run_id in result.err
    assert service.result(run_id)['ended'] is None
