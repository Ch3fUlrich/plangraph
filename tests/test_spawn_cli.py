import asyncio
import json
import subprocess
import time
import uuid

from plangraph.spawner.cli import main
from plangraph.spawner.service import SpawnService
from plangraph.spawner.runner import SpawnerRunner, kill_process_tree
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.leg_state import LegStateStore
import pytest
import sys
import os



@pytest.fixture(autouse=True)
def _gateway_url(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_GATEWAY_URL', 'http://127.0.0.1:9/v1')  # hermetic: I6 refuses a gateway leg with no base URL

@pytest.fixture(autouse=True)
def fake_host_binaries(tmp_path, monkeypatch):
    """Keep real probes (including worker processes) off the host installation."""
    from plangraph.planner.legs import DEFAULT_LEGS
    directory = tmp_path / 'bin'
    directory.mkdir()
    for name in ('opencode', 'agy'):
        models = '\n'.join(leg.model for leg in DEFAULT_LEGS if leg.harness == name)
        binary = directory / name
        binary.write_text('#!' + sys.executable + '\nimport sys\n'
                          'if sys.argv[1:] == ["models"]:\n'
                          '    print(' + repr(models) + ')\n'
                          'else:\n    raise SystemExit("unexpected real harness launch")\n')
        binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(directory) + os.pathsep + os.environ['PATH'])


def test_gateway_envelope_and_redaction(tmp_path):
    async def run():
        runner = SpawnerRunner()
        calls = []
        runner.provider_response = lambda *a, **kw: calls.append(a)
        envelope = {'autoos_provider_response': {'leg': 'cx', 'status': 429,
                    'body': 'quota exhausted', 'headers': {}}}
        async def factory(*args, **kwargs):
            envelope['autoos_provider_response']['nonce'] = json.loads(kwargs['env']['OMNIROUTE_ENVELOPE_HEADERS'])['X-OmniRoute-Run-Nonce']
            code = 'import sys; print("sk-plantedsecret Bearer plantedtoken API_KEY=hidden"); print(' + repr(json.dumps(envelope)) + ', file=sys.stderr)'
            return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)
        runner.process_factory = factory
        job = await runner.spawn('task', wait_seconds=3)
        assert len(calls) == 1
        assert 'plantedsecret' not in job.stdout
        assert 'plantedtoken' not in job.stdout
        assert 'hidden' not in job.stdout
    asyncio.run(run())


@pytest.mark.parametrize('stderr', ['429 Too Many Requests',
    json.dumps({'autoos_provider_response': {'leg': 'cx', 'status': 200, 'body': 'ok', 'headers': {}}}),
    json.dumps({'autoos_provider_response': {'leg': 'cx', 'status': 500, 'body': 'error', 'headers': {}}})])
def test_non_evidence(stderr):
    async def run():
        calls = []
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c',
                'import sys; print("429"); print(' + repr(stderr) + ', file=sys.stderr)', **kwargs)
        runner = SpawnerRunner(process_factory=factory)
        runner.provider_response = lambda *a, **kw: calls.append(a)
        await runner.spawn('task', wait_seconds=3)
        assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize('wait', [0, .05])
def test_bounded_wait_and_private_record(tmp_path, wait):
    async def run():
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c',
                'import time; from pathlib import Path\n'
                f'go = Path({str(tmp_path / "go")!r})\n'
                'deadline = time.monotonic() + 20\n'
                'while not go.exists() and time.monotonic() < deadline: time.sleep(.02)\n'
                'print("sk-plantedsecret API_KEY=hidden")', **kwargs)
        service = spawn_service(SpawnerRunner(process_factory=factory), tmp_path)
        started = asyncio.get_running_loop().time()
        record = await service.spawn(card={'estimate_tokens': 1000}, wait_seconds=wait)
        assert asyncio.get_running_loop().time() - started < 5
        assert record['status'] == 'running'
        (tmp_path / 'go').touch()
        await service.tasks[record['run_id']]
        path = service._path(record['run_id'])
        assert path.stat().st_mode & 0o777 == 0o600
        assert 'plantedsecret' not in path.read_text()
        assert 'hidden' not in path.read_text()
        assert not path.with_suffix('.tmp').exists()
    asyncio.run(run())


def test_harness_quota_falls_through(tmp_path, monkeypatch):
    from plangraph.spawner import resolver
    from test_spawner_exit_outcome import QUOTA
    rows = resolver.get_catalog_models()
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        dict(row, available=1, active=True) for row in rows])
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)

    async def run():
        store = LegStateStore(tmp_path / 'legs.sqlite')
        calls = []
        runner = SpawnerRunner()

        async def factory(*args, **kwargs):
            calls.append(list(runner.jobs.values())[-1].model_or_combo)
            code = ('import sys; print(' + repr(QUOTA) + ', file=sys.stderr); sys.exit(3)'
                    if len(calls) == 1 else 'print("done")')
            return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)

        runner.process_factory = factory
        service = spawn_service(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, wait_seconds=None)
        assert record['status'] == 'completed'
        assert len(calls) == 2
        assert record['attempts'][0]['skip_reason'].startswith('harness_limit:')
        model = calls[0] if '/' in calls[0] else 'agy/' + calls[0]
        state = store.leg_status(model, now=1000)
        assert state['state'] == 'limited'
        assert state['evidence']['reset_at'] == 39426

    asyncio.run(run())


def test_replan_and_all_limited(tmp_path, monkeypatch):
    from plangraph.spawner import resolver
    rows = resolver.get_catalog_models()
    # Exercise quota fallback independently of the host catalog's admission state.
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        dict(row, available=1, active=True) for row in rows])
    async def run():
        store = LegStateStore(tmp_path / 'legs.sqlite')
        runner = SpawnerRunner()
        async def factory(*args, **kwargs):
            model = list(runner.jobs.values())[-1].model_or_combo
            if len(runner.jobs) <= 2:
                runner.provider_response(list(runner.jobs)[-1], model,
                    429 if len(runner.jobs) == 1 else 402, 'provider quota exhausted', {}, store=store,
                    source='gateway_adapter', adapter_token=runner._adapter_tokens[list(runner.jobs)[-1]])
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'print("done")', **kwargs)
        runner.process_factory = factory
        service = spawn_service(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, wait_seconds=None)
        models = [a['leg']['model'] for a in record['attempts']]
        assert 'error' not in record, record.get('error')
        assert len(models) == 8, record
        assert len(runner.jobs) == 3
        assert record['attempts'][3]['skip_reason'] == 'earlier_leg_succeeded'
        assert 'gemini' in models[0] and 'mimo' in models[1] and 'openrouter' in models[2]
        for leg in service.route({'estimate_tokens': 1000}).legs:
                store.record_provider_response(leg.model if '/' in leg.model else 'agy/' + leg.model,
                    429, 'provider quota exhausted', {}, 'test')
        count = len(runner.jobs)
        record = await service.spawn(card={'estimate_tokens': 1000}, wait_seconds=None)
        assert record['exit_code'] == 4
        assert record['evidence']
        assert len(record['attempts']) == 8
        assert all(a['status'] == 'skipped' and a['skip_reason'] == 'limited'
                   for a in record['attempts'])
        assert len(runner.jobs) == count
    asyncio.run(run())


def test_cancel_injected_windows_killer(tmp_path):
    async def run():
        calls = []
        async def killer(pid):
            calls.append(pid)
        runner = SpawnerRunner(tree_killer=killer)
        from plangraph.spawner.runner import JobInfo
        runner.jobs['fake'] = JobInfo('fake', 'running', '', '')
        class Process:
            pid = 123
        runner._processes['fake'] = Process()
        assert (await runner.cancel('fake'))['status'] == 'cancelled'
        assert calls == [123]
    asyncio.run(run())


@pytest.mark.parametrize('wait', ['0', '0.05'])
def test_cli_wait_detaches_sleeping_child(tmp_path, monkeypatch, capsys, wait):
    # Fake the supervisor process, but keep its real sleeping child and monitor.
    import plangraph.spawner.cli as cli
    import threading
    import io
    threads = []
    class Supervisor:
        def __init__(self, *a, **kw):
            self.stdin = io.StringIO()
            self.stdout = self
        def readline(self):
            payload = json.loads(self.stdin.getvalue())
            ready = threading.Event()
            result = []
            def worker():
                async def run():
                    async def factory(*a, **kw):
                        return await asyncio.create_subprocess_exec(sys.executable, '-c',
                            'import signal, time\n'
                            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'   # Q21/M14
                            'time.sleep(.4); print("done")', **kw)
                    service = spawn_service(SpawnerRunner(process_factory=factory), payload.pop('state'))
                    record = await service.spawn(**payload)
                    result.append(record)
                    ready.set()
                    await service.tasks[record['run_id']]
                asyncio.run(run())
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
            assert ready.wait(3)
            return json.dumps(result[0])
        def close(self):
            pass
    class Input(io.StringIO):
        def close(self):
            pass
    old_init = Supervisor.__init__
    def init(self, *a, **kw):
        old_init(self, *a, **kw)
        self.stdin = Input()
    Supervisor.__init__ = init
    monkeypatch.setattr(cli, '_start_supervisor', Supervisor)
    import time
    started = time.monotonic()
    assert main(['--state', str(tmp_path), 'spawn', 'task', '--card',
                 '{"estimate_tokens":1000}', '--wait', wait]) == 0
    assert time.monotonic() - started < float(wait) + 1   # Q21/M14: within ~wait_seconds+1
    record = json.loads(capsys.readouterr().out)
    assert record['status'] == 'running'
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert spawn_service(state=tmp_path).result(record['run_id'])['status'] == 'completed'


def test_route_and_refusal(tmp_path, capsys):
    assert main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":1000}', '--role', 'implement', '--json']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['first_leg']['model'] == 'gemini-3.8-flash'
    assert set(plan) == {'legs', 'first_leg', 'skipped', 'p_source', 'review_seats', 'reasons', 'error', 'gaps', 'candidates'}
    assert main(['--state', str(tmp_path), 'spawn', 'task']) == 2
    assert 'spawn by card' in capsys.readouterr().err


def test_records_and_review(tmp_path):
    async def run():
        service = SpawnService(state=tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', dry_run=True)
        assert service.result(record['run_id'])['status'] == 'dry_run'
        assert service.ps()['runs'][0]['run_id'] == record['run_id']
        plan = service.route({'estimate_tokens': 1000}, role='review', review_of=record['run_id'])
        assert all(l.family != record['writer_family'] for l in plan.legs)
    asyncio.run(run())


@pytest.mark.parametrize('provider_stop', [True, False])
def test_transport_only_fallthrough(tmp_path, provider_stop):
    async def run():
        store = LegStateStore(tmp_path / 'legs.sqlite')
        runner = SpawnerRunner()
        async def factory(*args, **kwargs):
            if provider_stop and len(runner.jobs) == 1:
                job_id = next(iter(runner.jobs))
                first_model = runner.jobs[job_id].model_or_combo
                runner.provider_response(job_id, first_model, 429,
                    'provider quota exhausted', {}, store=store, source='gateway_adapter',
                    adapter_token=runner._adapter_tokens[job_id])
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'print("429")', **kwargs)
        runner.process_factory = factory
        service = SpawnService(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=None)
        assert len(record['attempts']) == 8
        launched = [a for a in record['attempts'] if a['status'] != 'skipped']
        assert len(launched) == (2 if provider_stop else 1)
        assert len(runner.jobs) == len(launched)
        assert all(a['skip_reason'] == 'earlier_leg_succeeded'
                   for a in record['attempts'][len(launched):])
        first_model = list(runner.jobs.values())[0].model_or_combo
        first_key = first_model if '/' in first_model else 'agy/' + first_model
        assert store.leg_status(first_key)['state'] == ('limited' if provider_stop else 'ok')
        if provider_stop:
            assert service.route({'estimate_tokens': 1000}).first_leg.model != first_model
    asyncio.run(run())


def test_breaker_503_fails_run_without_same_leg_retry(tmp_path):
    """D-2791: a gateway breaker 503 / circuit-open counts as a failed run.

    It is never retried in a loop on the same leg and is not a provider stop
    (only 429/402 advance the leg state); the next writer picks the card up.
    """
    async def run():
        store = LegStateStore(tmp_path / 'legs.sqlite')
        runner = SpawnerRunner()
        async def factory(*args, **kwargs):
            job_id = next(iter(runner.jobs))
            runner.provider_response(job_id, runner.jobs[job_id].model_or_combo, 503,
                'circuit open', {}, store=store, source='gateway_adapter',
                adapter_token=runner._adapter_tokens[job_id])
            return await asyncio.create_subprocess_exec(sys.executable, '-c',
                'import sys; print("503 circuit open", file=sys.stderr); raise SystemExit(1)', **kwargs)
        runner.process_factory = factory
        service = SpawnService(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=None)
        launched = [a for a in record['attempts'] if a['status'] != 'skipped']
        assert len(launched) == 1
        assert len(runner.jobs) == 1
        assert record['status'] == 'failed'
        assert not runner.provider_responses
        first_model = list(runner.jobs.values())[0].model_or_combo
        first_key = first_model if '/' in first_model else 'agy/' + first_model
        assert store.leg_status(first_key)['state'] == 'ok'
    asyncio.run(run())


def test_windows_killer():
    async def run():
        calls = []
        class Process:
            async def communicate(self):
                return b'', b''
        async def factory(*args, **kwargs):
            calls.append(args)
            return Process()
        await kill_process_tree(123, platform='nt', command_factory=factory)
        assert calls == [('taskkill', '/PID', '123', '/T', '/F')]
    asyncio.run(run())


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process group integration')
def test_cancel_real_child(tmp_path):
    async def run():
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        runner = SpawnerRunner(process_factory=factory)
        service = SpawnService(runner, tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=0.05)
        assert 'job_id' in record, record
        process = runner._processes[record['job_id']]
        assert process.pid and process.returncode is None
        assert (await service.cancel(record['run_id']))['status'] == 'cancelled'
        await asyncio.wait_for(process.wait(), 3)
        assert process.returncode != 0
    asyncio.run(run())


def test_override_and_unknown_review(tmp_path):
    async def run():
        service = SpawnService(state=tmp_path)
        with pytest.raises(ValueError, match='unknown run'):
            await service.spawn(card={'estimate_tokens': 1000}, review_of='run-unknown', dry_run=True)
        await service.spawn(card={'estimate_tokens': 1000}, model='mimo/mimo-v2.6-pro', override_reason='explicit test override', dry_run=True)
        assert json.loads((tmp_path / 'spawn-overrides.jsonl').read_text())['reason'] == 'explicit test override'
        with pytest.raises(ValueError):
            await service.spawn(model='omniroute/mimo-v2.6-pro', override_reason='explicit test override', dry_run=True)
    asyncio.run(run())


def test_cli_wait_returns_after_the_dispatcher_died(tmp_path):
    """Q21/M14: `wait` must return once the supervisor that owned the run is
    gone (its child may ignore SIGTERM and hold its group), not spin on 'running'."""
    run_id = 'run-' + uuid.uuid4().hex
    log = tmp_path / ('supervisor-' + uuid.uuid4().hex + '.stderr.log')
    child = subprocess.Popen([sys.executable, '-c',
        'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'],
        start_new_session=True)
    try:
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        (tmp_path / (log.name + '.pid')).write_text(str(dead.pid))
        spawn_service(state=tmp_path)._save(dict(
            run_id=run_id, status='running', started=time.time(), ended=None, attempts=[],
            job_id='j-' + run_id, pid=child.pid, supervisor_stderr_path=str(log)))
        started = time.monotonic()
        out = subprocess.run([sys.executable, '-m', 'plangraph.spawner.cli', '--state',
                              str(tmp_path), 'wait', run_id],
                             capture_output=True, text=True, timeout=8)
        assert time.monotonic() - started < 3
        final = json.loads(out.stdout)
        assert final['status'] != 'running' and final['ended'] is not None
    finally:
        child.kill()
        child.wait()
