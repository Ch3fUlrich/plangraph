"""Launch-boundary reasoning retries, terminal records, and cancellation guards."""
from types import SimpleNamespace

import pytest

from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def launch_leg_case(tmp_path):
    """Observe the shared launch boundary independently of caller final saves."""
    import json
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner.runner import JobInfo

    chosen = next(l for l in DEFAULT_LEGS if l.id == 'mimo-v2.6-flash')
    jobs = [JobInfo(name, 'failed', 'review', chosen.model, role='review', exit_code=1,
                   events=[dict(event='opencode_reasoning_exhausted', cap_available=True)])
            for name in ('first', 'second')]
    calls = []
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(_processes={}, _tasks={}, _owned={}, default_cwd=str(tmp_path))
    seat._running_records = {}
    seat._path = lambda run_id: tmp_path / 'record.json'
    seat._save = lambda record: seat._path(record['run_id']).write_text(json.dumps(record))
    record = dict(run_id='run-test', role='review', status='running', started=100,
                  attempts=[], events=[], writer_family='original', given_writer_families=['gpt'])

    async def spawn(**options):
        job = jobs[len(calls)]
        calls.append(options)
        return job

    seat.runner.spawn = spawn
    return seat, record, chosen, jobs, calls


@pytest.mark.parametrize('scenario', ['no-cap', 'already-retried', 'twice', 'render-refused'])
def test_reasoning_terminal_saved_at_launch_boundary(launch_leg_case, monkeypatch, scenario):
    import asyncio
    import json

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    if scenario == 'no-cap':
        jobs[0].events[0]['cap_available'] = False
    if scenario == 'already-retried':
        record['reasoning_retry'] = 1
    if scenario == 'render-refused':
        jobs[1].events = [dict(event='render_refused', reason='reasoning_output_limit: unavailable')]
    job = asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100}))
    saved = json.loads(seat._path(record['run_id']).read_text())
    assert saved['status'] == job.status == 'failed'
    assert saved['reason'] == 'reasoning_output_limit' and saved['exit_code'] != 0
    assert len(saved['attempts']) == len(calls) == (2 if scenario in ('twice', 'render-refused') else 1)
    assert all(a['events'] for a in saved['attempts'])
    assert saved['events'][-1] == {'event': 'reasoning_output_limit'}


@pytest.mark.parametrize('remaining', [0, 0.01, 30, 30.01])
def test_reasoning_retry_needs_useful_budget(launch_leg_case, monkeypatch, remaining):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time',
                        SimpleNamespace(time=lambda: 200 - remaining))
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100, 'hard_max': True}))
    assert len(calls) == (2 if remaining > 30 else 1)
    if remaining > 30:
        assert calls[1]['watchdog'] == {'max_s': pytest.approx(remaining), 'hard_max': True}


@pytest.mark.parametrize('guard', ['timeout', 'timed_out', 'stalled', 'cancelled', 'watchdog'])
def test_reasoning_retry_respects_late_terminal_mapping(launch_leg_case, monkeypatch, guard):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    if guard == 'watchdog':
        jobs[0].events.append(dict(event='watchdog_timeout', rule='max'))
    else:
        record.update(status=guard, reason=guard)
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100}))
    assert len(calls) == 1 and record.get('reason') != 'reasoning_output_limit'


@pytest.mark.parametrize('guard', ['cancelled', 'timed_out', 'kill_incomplete'])
def test_retry_render_refusal_cannot_override_terminal_guard(launch_leg_case, monkeypatch, guard):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    jobs[1].events = [dict(event='render_refused', reason='reasoning_output_limit: unavailable')]
    original = seat.runner.spawn

    async def spawn(**options):
        job = await original(**options)
        if options.get('reasoning_cap'):
            if guard == 'cancelled':
                seat._path(record['run_id']).with_name('cancelled').touch()
            elif guard == 'kill_incomplete':
                job.survivors = [123]
            else:
                record.update(status=guard, reason=guard)
        return job

    seat.runner.spawn = spawn
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100}))
    assert len(calls) == 2 and record.get('reason') != 'reasoning_output_limit'


@pytest.mark.parametrize('exhausted', [False, True])
def test_cancel_marker_only_changes_exhausted_leg(launch_leg_case, exhausted):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    if not exhausted:
        jobs[0].events = []
    seat._path(record['run_id']).with_name('cancelled').touch()
    job = asyncio.run(seat._launch_leg(record, chosen, prompt_record=None))
    assert job.status == ('cancelled' if exhausted else 'failed')
    assert len(calls) == 1 and record.get('reason') != 'reasoning_output_limit'


def test_normal_base_launch_keeps_original_schema(launch_leg_case):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    record['role'], jobs[0].events = 'orchestrate', []
    seat.runner._owned['first'] = dict(pid=1, pid_start=2, job_object=True,
                                     job_name='owned', escapees=[], extra_owned='private')
    before = dict(record)
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None))
    stamped = {key: record[key] for key in ('harness', 'harness_version', 'harness_fallback', 'host')}
    assert stamped['harness'] == chosen.harness   # the launch writes the harness fields (HARNESS-RECORDED-PER-RUN)
    assert record == dict(before, job_id='first', pid=None, pid_start=2, job_name='owned',
                          escapees=[], cwd=seat.runner.default_cwd, **stamped)


@pytest.mark.parametrize('cancel', ['outer', 'inner'])
def test_base_launch_preserves_shield_cancellation(launch_leg_case, cancel):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    record['role'], jobs[0].events = 'orchestrate', []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def monitor():
            entered.set()
            await release.wait()

        task = seat.runner._tasks['first'] = asyncio.create_task(monitor())
        launch = asyncio.create_task(seat._launch_leg(record, chosen, prompt_record=None))
        await entered.wait()
        # A save means launch has reached the monitor await, without timing sleeps.
        while not seat._path(record['run_id']).exists():
            await asyncio.sleep(0)
        (launch if cancel == 'outer' else task).cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(launch, 1)
            if cancel == 'outer':
                assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, launch, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize('writer', ['codex', 'cx-gpt-6.2-sol', 'gpt'])
def test_review_launch_sends_resolved_author_family(launch_leg_case, writer):
    import asyncio
    seat, record, chosen, jobs, calls = launch_leg_case
    record['given_writer_families'] = [writer]
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None))
    for call in calls:
        assert call['headers']['X-OmniRoute-Author-Model'] == 'gpt'
        assert call['headers']['X-OmniRoute-Exclude-Family'] == 'gpt'
        assert call['headers']['X-OmniRoute-Task-Role'] == 'review'


def test_review_launch_refuses_placeholder_author(launch_leg_case):
    import asyncio
    from plangraph.routing_errors import SpawnRefused
    seat, record, chosen, jobs, calls = launch_leg_case
    record['given_writer_families'] = ['author']
    with pytest.raises(SpawnRefused, match='unknown writer family.*accepted names:'):
        asyncio.run(seat._launch_leg(record, chosen, prompt_record=None))
    assert calls == []


@pytest.mark.parametrize('outcome', [
    'completed', 'launch_failed', 'cancelled', 'ordinary_review', 'orchestration_only',
    'disk_low', 'memory_low',
])
def test_worker_review_releases_exact_checkout_and_build_target(tmp_path, monkeypatch, outcome):
    import asyncio
    from pathlib import Path
    import subprocess
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import host_ready, overflow, seat_spawn
    from plangraph.spawner.notify import completion_event
    from plangraph.spawner.prompt_input import checkout_env
    from plangraph.spawner.runner import JobInfo
    from plangraph.spawner.runner import spawn_service

    receiver = tmp_path / 'receiver'
    receiver.mkdir()

    def git(*args):
        return subprocess.check_output(['git', '-C', str(receiver), *args],
                                       env=checkout_env(), text=True).strip()

    git('init', '-qb', 'main')
    identity = ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid')
    git(*identity, 'commit', '--allow-empty', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    git('checkout', '--detach', base)
    (receiver / 'source').write_text('head')
    git('add', 'source')
    git(*identity, 'commit', '-qm', 'head')
    head = git('rev-parse', 'HEAD')
    git('checkout', '--detach', base)
    assert not (receiver / 'source').exists()
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'opencode-go-deepseek-v4-flash')
    monkeypatch.delenv('PG_URL', raising=False)
    monkeypatch.delenv('PG_TOKEN_FILE', raising=False)
    monkeypatch.delenv('PLANGRAPH_PLACED_HOST', raising=False)
    monkeypatch.setenv('PLANGRAPH_HOST_NAME', 'worker')
    monkeypatch.setenv('PLANGRAPH_ADMISSION_WAIT_S', '0')
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {'hosts': []})
    monkeypatch.setattr(overflow, 'placement_config', lambda *_: {
        'hosts': {'worker': {'orchestration_only': True}} if outcome == 'orchestration_only' else {},
        'public_repos': [],
    })
    monkeypatch.setattr(seat_spawn, 'resources', lambda _: (0, 0 if outcome == 'memory_low' else 50000))
    monkeypatch.setattr(host_ready, 'local_disk_admission', lambda *_args, **_opts: (
        None, 'disk_low: fixture' if outcome == 'disk_low' else None))
    service = spawn_service(state=tmp_path / 'state')
    service.runner.default_cwd = str(receiver)
    async def admit(*args, **kwargs):
        return True
    monkeypatch.setattr('plangraph.spawner.service.admit_base', admit)
    monkeypatch.setattr('plangraph.spawner.service.writer_launch',
                        lambda prompt, *_: (prompt, {'FIXTURE_HARNESS_ENV': 'keep'}))
    monkeypatch.setattr(service, '_annotate_spawn', lambda *args: None)
    async def prompt(*args, **kwargs):
        return 'fixture'
    monkeypatch.setattr(service, '_prepare_leg_prompt', prompt)
    monkeypatch.setattr(service, '_continue_writer', admit)
    ready = asyncio.Event()
    paths = []

    async def launch(chosen, cwd, **options):
        checkout = Path(cwd)
        assert checkout != receiver
        assert (checkout / 'source').read_text() == 'head'
        assert options['extra_env']['FIXTURE_HARNESS_ENV'] == 'keep'
        target = Path(options['extra_env']['CARGO_TARGET_DIR'])
        assert checkout.parent == target.parent
        assert checkout.parent.is_relative_to(service.state)
        target.mkdir(exist_ok=True)
        (target / 'build-output').touch()
        paths.extend([checkout, target])
        ready.set()
        if outcome == 'launch_failed':
            raise OSError('fixture launch failed')
        if outcome == 'cancelled':
            await asyncio.Future()
        return JobInfo('fixture', 'completed', 'review', chosen.model, role='review', exit_code=0,
                       cwd=cwd, stdout='Reviewed source.\nFILES READ: ./source\nVERDICT: ACCEPT\n')

    monkeypatch.setattr(service.runner, 'spawn', launch)
    async def run():
        card = {'review_workspace': True, 'repository_review_required': True, 'item_key': 'demo:ITEM',
                'diff_ref': {'repo': str(receiver), 'base': base, 'head': head}}
        if outcome != 'ordinary_review':
            card['native_writer_run'] = 'run-writer'
        record = await service.spawn(
            card=card,
            prompt='fixture', role='review', writer_families=('openai',),
            _plan=SpawnPlan((leg,), (), 'fixture', (), ()))
        refusal = {
            'ordinary_review': 'review workspace requires a registered worker host',
            'orchestration_only': 'review workspace requires a registered worker host',
            'disk_low': 'disk_low: fixture',
            'memory_low': 'memory:',
        }.get(outcome)
        if refusal:
            assert record['status'] == 'refused'
            assert refusal in record['reason']
            assert not paths
            return
        assert record['status'] == 'running', record.get('reason')
        task = service.tasks[record['run_id']]
        if outcome == 'cancelled':
            await asyncio.wait_for(ready.wait(), 5)
            await service.cancel(record['run_id'])
        await asyncio.gather(task, return_exceptions=True)
        final = service.result(record['run_id'])
        assert final['status'] == {'launch_failed': 'failed'}.get(outcome, outcome), final.get('error')
        assert final['head_sha'] == head
        if outcome == 'completed':
            assert final['cwd'] == str(paths[0])
        assert final['host'] == 'worker'
        assert final['item_key'] == 'demo:ITEM'
        journal = service.state / 'runs' / record['run_id'] / 'verdict.json'
        import json
        delivery = json.loads(journal.read_text())
        assert delivery['status'] == 'skipped'
        assert delivery['reason'] == 'native writer review precedes Keeper seat admission'
        assert (delivery['item_key'], delivery['host'], delivery['head_sha']) == ('demo:ITEM', 'worker', head)
        assert len(paths) == 2 and all(not path.exists() for path in paths)
        assert completion_event(final, paths[0])['head_sha'] == head
        assert git('rev-parse', 'HEAD') == base

    asyncio.run(run())


@pytest.mark.parametrize("case", ["admitted", "missing-cwd", "refused", "down", "dry-run", "native-writer"])
def test_seat_admission_at_launch_boundary(launch_leg_case, monkeypatch, case):
    import asyncio
    import json
    from urllib.error import HTTPError, URLError

    from plangraph.routing_errors import SpawnRefused
    from plangraph.spawner.notify import KeeperSink

    seat, record, chosen, jobs, calls = launch_leg_case
    jobs[0].events = []
    record["item_key"] = "demo:A"
    if case == "native-writer":
        record["native_writer_run"] = "run-writer"
    if case != "missing-cwd":
        record["cwd"] = seat.runner.default_cwd
    binding = {"key": "demo:A", "epoch": 1, "round": 2, "head": "a" * 40, "seat_run": record["run_id"]}
    admissions = []
    monkeypatch.setenv("PG_URL", "http://keeper.invalid")
    monkeypatch.setenv("PG_TOKEN_FILE", "fixture-token")

    def admit(self, pending, cwd):
        admissions.append(cwd)
        assert not seat._path(pending["run_id"]).exists() and not calls
        if case == "refused":
            raise HTTPError(self.url, 409, "stale", {}, None)
        if case == "down":
            raise URLError("unreachable")
        pending["request"] = {"verdict_binding": binding}

    spawn = seat.runner.spawn

    async def inspect_launch(**options):
        path = seat._path(record["run_id"])
        if case in ("dry-run", "native-writer"):
            assert not path.exists()
        else:
            assert json.loads(path.read_text())["request"]["verdict_binding"] == binding
        return await spawn(**options)

    monkeypatch.setattr(KeeperSink, "admit_seat_verdict", admit)
    seat.runner.spawn = inspect_launch
    launch = seat._launch_leg(record, chosen, prompt_record=None, dry_run=case == "dry-run")
    if case in ("refused", "down"):
        with pytest.raises(SpawnRefused, match="seat admission failed"):
            asyncio.run(launch)
        assert not calls and not seat._path(record["run_id"]).exists()
    else:
        assert asyncio.run(launch) is jobs[0]
        assert len(calls) == 1
    assert admissions == ([] if case in ("dry-run", "native-writer") else [seat.runner.default_cwd])
