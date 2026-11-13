"""D-5661: local bare origins, fake processes, and mandatory spawn admission."""
import asyncio
import inspect
import json
import os
import subprocess

import pytest


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
        'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', *args],
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    for key in ('GIT_CONFIG_PARAMETERS', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        monkeypatch.delenv(key, raising=False)
    origin, repo = tmp_path / 'origin.git', tmp_path / 'repo'
    git(tmp_path, 'init', '--bare', '-b', 'main', str(origin))
    git(tmp_path, 'clone', str(origin), str(repo))
    git(repo, 'commit', '--allow-empty', '-m', 'initial')
    git(repo, 'push', 'origin', 'HEAD:main')
    git(repo, 'remote', 'set-head', 'origin', 'main')
    return origin, repo


def advance(origin, repo, count=2):
    old = git(repo, 'rev-parse', 'HEAD')
    upstream = repo.parent / 'upstream'
    git(repo.parent, 'clone', str(origin), str(upstream))
    for _ in range(count):
        git(upstream, 'commit', '--allow-empty', '-m', 'advance')
    git(upstream, 'push', 'origin', 'HEAD:main')
    return old


def test_base_fresh_contract(repository, monkeypatch):
    from plangraph.spawner.base_fresh import base_fresh
    origin, repo = repository
    sha = git(origin, 'rev-parse', 'main')
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        monkeypatch.setenv(name, str(repo / 'nonexistent'))
    assert base_fresh(repo) == dict(ok=True, reason=None, repo=str(repo),
        default_branch='main', origin_sha=sha, behind=0, base_sha=sha)
    assert list(inspect.signature(base_fresh).parameters) == ['repo', 'default', 'fetch', 'timeout']
    assert base_fresh.__kwdefaults__ == dict(default=None, fetch=True, timeout=60)


def test_unmarked_tests_keep_real_gate(tmp_path):
    from plangraph.spawner import base_fresh
    for check in (base_fresh.base_fresh, base_fresh._check):
        result = check(tmp_path)
        assert not result['ok'] and result['reason'] == 'base_stale: fetch_failed'
        assert result['origin_sha'] is None


@pytest.mark.parametrize('refspec', ['', '+refs/heads/side:refs/remotes/origin/side'])
def test_fetch_refreshes_tracking_ref_without_matching_remote_refspec(repository, refspec):
    from plangraph.spawner.base_fresh import base_fresh
    origin, repo = repository
    config = repo / '.git' / 'config'
    config.write_text(config.read_text().replace('+refs/heads/*:refs/remotes/origin/*', refspec)
                      .replace('\tfetch = \n', ''))
    old = advance(origin, repo)
    assert git(repo, 'rev-parse', 'origin/main') == old
    result = base_fresh(repo)
    assert not result['ok'] and result['reason'] == 'base_stale: behind=2'
    assert result['origin_sha'] == git(origin, 'rev-parse', 'main')
    assert git(repo, 'rev-parse', 'origin/main') == result['origin_sha']


@pytest.mark.parametrize('failures', [1, 2])
def test_fetch_ref_lock_retries_once_then_fails_closed(repository, monkeypatch, failures):
    from plangraph.spawner import base_fresh
    origin, repo = repository
    advance(origin, repo)
    run, calls = subprocess.run, []
    def locked(args, **kwargs):
        if 'fetch' in args:
            calls.append(args)
            if len(calls) <= failures:
                raise subprocess.CalledProcessError(1, args, stderr='cannot lock ref')
        return run(args, **kwargs)
    monkeypatch.setattr(base_fresh.subprocess, 'run', locked)
    result = base_fresh.base_fresh(repo)
    assert len(calls) == 2 and not result['ok']
    assert result['reason'] == ('base_stale: behind=2' if failures == 1 else 'base_stale: fetch_failed')
    assert result['origin_sha'] == (git(origin, 'rev-parse', 'main') if failures == 1 else None)


def test_default_branch_and_fallback(repository):
    from plangraph.spawner.base_fresh import base_fresh
    _, repo = repository
    git(repo, 'push', 'origin', 'HEAD:trunk')
    git(repo, 'fetch', 'origin', 'trunk')
    git(repo, 'remote', 'set-head', 'origin', 'trunk')
    assert base_fresh(repo)['default_branch'] == 'trunk'
    assert base_fresh(repo, default='main')['default_branch'] == 'main'
    git(repo, 'symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
    assert base_fresh(repo)['default_branch'] == 'main'


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
            return (b'{"type":"text","part":{"text":"finished"}}\n'
                    b'{"type":"step_finish","part":{"reason":"stop"}}\n'), b''
    async def factory(*args, **kwargs):
        calls.append(kwargs)
        return Process()
    async def ready(*args, **kwargs):
        return None
    async def planned(plan, **kwargs):
        return plan
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', ready)
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', planned)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', ready)
    monkeypatch.setattr('plangraph.spawner.seat_spawn.resources', lambda service: (0, 1000000))
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'cx-gpt-6.1-sol-xhigh')
    def create(hardened):
        runner = SpawnerRunner(default_cwd=str(repository[1]), process_factory=factory)
        service = (spawn_service if hardened else SpawnService)(runner=runner, state=tmp_path / 'state')
        monkeypatch.setattr(service, '_route', lambda *a, **k: SpawnPlan((chosen,), (), 'test', (), ()))
        return service
    return create, calls


@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('role', ['implement', 'orchestrate', 'research', 'writer', 'fix'])
@pytest.mark.parametrize('stale', [False, True])
def test_every_worker_role(repository, dispatch, hardened, role, stale):
    origin, repo = repository
    if stale:
        advance(origin, repo)
    service, calls = dispatch[0](hardened), dispatch[1]
    card = {'estimate_tokens': 100}
    record = asyncio.run(service.spawn(card, 'Implement a tested change', role=role, wait_seconds=None))
    assert service.result(record['run_id']) == record
    if stale:
        assert record['status'] == 'failed' and record['exit_code'] != 0
        assert record['reason'] == 'base_stale: behind=2' and not calls
    else:
        assert record['status'] == 'completed' and len(calls) == 1
        assert record['base_sha'] == card['base_sha'] == git(origin, 'rev-parse', 'main')


@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('decision', [None, 'D-123', 'D-no', 'D-123\n', 123])
def test_decision_and_fetch_failure(repository, dispatch, hardened, decision):
    origin, repo = repository
    advance(origin, repo)
    service, calls = dispatch[0](hardened), dispatch[1]
    card = {'estimate_tokens': 100, 'base_decision': decision}
    record = asyncio.run(service.spawn(card, 'Implement a tested change', wait_seconds=None))
    assert (record['status'] == 'completed') == (decision == 'D-123')
    assert len(calls) == int(decision == 'D-123')
    if decision == 'D-123':
        assert record['base_decision'] == decision
        assert record['base_sha'] == card['base_sha'] == git(origin, 'rev-parse', 'main')
    git(repo, 'remote', 'set-url', 'origin', str(repo / 'missing-origin'))
    calls.clear()
    record = asyncio.run(service.spawn(card, 'Implement a tested change', wait_seconds=None))
    assert record['status'] == 'failed' and record['exit_code'] != 0
    assert record['reason'] == 'base_stale: fetch_failed' and not calls


@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('current', [False, True, None])
@pytest.mark.parametrize('decision', [None, 'D-123', 'invalid'])
def test_review_diff_not_callers_head(repository, dispatch, hardened, role, current, decision):
    origin, repo = repository
    old = advance(origin, repo)
    git(repo, 'fetch', 'origin', 'main')
    git(repo, 'switch', '-c', 'lane', 'origin/main' if current else old)
    (repo / 'change.txt').write_text('reviewed change\n')
    git(repo, 'add', 'change.txt')
    git(repo, 'commit', '-m', 'reviewed lane')
    head = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'switch', '--detach', old if current is not False else 'origin/main')
    card = {'estimate_tokens': 100, 'base_decision': decision}
    if current is not None:
        card['diff_ref'] = dict(repo=str(repo), base=old, head=head)
    service, calls = dispatch[0](hardened), dispatch[1]
    record = asyncio.run(service.spawn(card, 'Review this change', role=role,
        writer_families=['deepseek'], dry_run=current is not False or decision == 'D-123', wait_seconds=None))
    assert not calls
    if current is False and decision != 'D-123':
        assert record['status'] == 'failed' and record['exit_code'] != 0
        assert record['reason'] == 'base_stale: behind=2'
    else:
        assert record['status'] == 'dry_run', record.get('error')
        if current is not None:
            assert record['base_sha'] == card['base_sha'] == git(origin, 'rev-parse', 'main')
        if current is False:
            assert record['base_decision'] == decision


def test_cli_no_env_flag_or_setting_bypass(repository, dispatch, monkeypatch, capsys, tmp_path):
    from plangraph.spawner import cli
    origin, repo = repository
    advance(origin, repo)
    service, calls = dispatch[0](True), dispatch[1]
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setenv('PLANGRAPH_BASE_FRESH', '0')
    monkeypatch.setenv('PLANGRAPH_SKIP_BASE_FRESH', '1')
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'base_fresh': False}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(settings))
    args = ['spawn', '--card', '{"estimate_tokens":100}', '--dry-run', 'Implement a tested change']
    assert cli.main(args) != 0
    assert json.loads(capsys.readouterr().out)['reason'] == 'base_stale: behind=2'
    assert not calls
    with pytest.raises(SystemExit) as exc:
        cli.main([*args, '--skip-base-fresh'])
    assert exc.value.code == 2


@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('mode', ['pinned', 'worktree', 'seats'])
def test_launch_variants_cannot_bypass(repository, dispatch, hardened, mode):
    origin, repo = repository
    old = advance(origin, repo)
    options = {'leg': 'cx-gpt-6.1-sol-xhigh', 'override_reason': 'hermetic base admission test'} if mode == 'pinned' else {'run_worktree': True} if mode == 'worktree' else {
        'seats': True, 'role': 'review', 'writer_families': ['deepseek']}
    card = {'estimate_tokens': 100}
    if mode == 'seats':
        card['diff_ref'] = dict(repo=str(repo), base=old, head=old)
        from dataclasses import replace
        service = dispatch[0](hardened)
        route = service._route
        service._route = lambda *a, **k: replace(route(*a, **k), review_seats=route(*a, **k).legs)
    else:
        service = dispatch[0](hardened)
    result = asyncio.run(service.spawn(card, 'Implement a tested change', wait_seconds=None, **options))
    for record in result.get('seats', [result]):
        assert record['status'] == 'failed' and record['reason'] == 'base_stale: behind=2'
    assert not dispatch[1]


def test_worktree_head_and_unfetched_remote(repository):
    from plangraph.spawner.base_fresh import base_fresh
    origin, repo = repository
    git(repo, 'worktree', 'add', '--detach', str(repo.parent / 'worker'), 'HEAD')
    old = advance(origin, repo)
    assert git(repo, 'rev-parse', 'origin/main') == old
    assert base_fresh(repo)['reason'] == 'base_stale: behind=2'
    git(repo, 'merge', '--ff-only', 'origin/main')
    assert base_fresh(repo)['ok']
    assert base_fresh(repo.parent / 'worker')['reason'] == 'base_stale: behind=2'
    git(repo, 'commit', '--allow-empty', '-m', 'lane ahead of main')
    assert base_fresh(repo)['ok']
    assert base_fresh(repo)['origin_sha'] == git(origin, 'rev-parse', 'main')


def test_fetch_timeout_refuses(repository, monkeypatch):
    from plangraph.spawner import base_fresh
    run = subprocess.run
    def timeout(args, **kwargs):
        if 'fetch' in args:
            assert args[-4:] == ['fetch', '-q', 'origin', '+refs/heads/main:refs/remotes/origin/main']
            assert kwargs['timeout'] == 60
            raise subprocess.TimeoutExpired(args, 60)
        return run(args, **kwargs)
    monkeypatch.setattr(base_fresh.subprocess, 'run', timeout)
    assert base_fresh.base_fresh(repository[1])['reason'] == 'base_stale: fetch_failed'


def test_cli_help_documents_detached_admission(capsys):
    from plangraph.spawner import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(['spawn', '--help'])
    assert exc.value.code == 0
    help_text = ' '.join(capsys.readouterr().out.split())
    assert 'Detached non-dry spawn returns 0 before admission' in help_text
    assert 'base_stale' in help_text and 'status/result' in help_text


def test_continuation_rechecks_base(repository, dispatch):
    origin, repo = repository
    service, calls = dispatch[0](False), dispatch[1]
    card = {'estimate_tokens': 100}
    record = asyncio.run(service.spawn(card, 'Implement a tested change', wait_seconds=None))
    assert len(calls) == 1
    advance(origin, repo)
    calls.clear()
    asyncio.run(service._continue_writer(record, 'Remaining gaps: finish the test'))
    child = service.result(record['continuation_run_id'])
    assert child['status'] == 'failed' and child['reason'] == 'base_stale: behind=2'
    assert not calls


def test_supervisor_returns_stale_exit(repository, dispatch, monkeypatch, capsys):
    import io
    from plangraph.spawner import cli
    advance(*repository)
    service = dispatch[0](True)
    monkeypatch.setattr(cli, 'SpawnService', lambda **k: service)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(dict(
        state=str(service.state), card={'estimate_tokens': 100}, prompt='Implement a tested change'))))
    class Output(io.StringIO):
        def close(self):
            pass
    output = Output()
    monkeypatch.setattr(cli.sys, 'stdout', output)
    assert cli.main(['--supervise']) == 1
    assert json.loads(output.getvalue())['reason'] == 'base_stale: behind=2'
    assert not dispatch[1]
