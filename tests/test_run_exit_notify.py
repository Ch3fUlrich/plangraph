import asyncio
import json
import sys

import pytest

from plangraph.spawner.runner import spawn_service
from plangraph.spawner.notify import FileInbox


@pytest.mark.parametrize('item', [None, 'test:item'])
def test_keeper_completion_skip_or_retry(tmp_path, monkeypatch, caplog, item):
    from plangraph.spawner.notify import KeeperSink
    from plangraph.spawner import private_io
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    sink = KeeperSink(url='https://keeper.invalid', token_file='unused')
    calls, writes = [], []
    def fail(recipient, event):
        calls.append((recipient, event))
        raise RuntimeError('delivery unavailable')
    monkeypatch.setattr(sink, 'deliver', fail)
    original = private_io.atomic_write_private
    def write(path, data):
        if path.name == 'keeper.delivered':
            writes.append(path)
        return original(path, data)
    monkeypatch.setattr(private_io, 'atomic_write_private', write)
    service = spawn_service(state=tmp_path, keeper_sink=sink)
    record = dict(run_id='run-keeper', status='completed', exit_code=0,
                  started=1, ended=2, item=item)
    service._save(record)
    marker = service._path(record['run_id']).parent / 'keeper.delivered'
    assert marker.exists() == (item is None)
    first_logs = list(caplog.records)
    service.retry_completions()
    parent = (tmp_path / 'inbox' / 'parent.jsonl').read_text().splitlines()
    assert len(parent) == 1
    if item is None:
        assert calls == []
        assert len(writes) == 1
        assert caplog.records == first_logs
        assert len(first_logs) <= 1
        assert not any(log.exc_info for log in first_logs)
    else:
        assert len(calls) == 2
        assert writes == []
        assert not marker.exists()
        assert len([log for log in caplog.records
                    if log.message == 'completion delivery failed: keeper' and log.exc_info]) == 2


def test_direct_keeper_no_item_still_refused():
    from plangraph.spawner.notify import KeeperSink
    with pytest.raises(ValueError, match='requires PLANGRAPH_SPAWN_ITEM'):
        KeeperSink(url='https://keeper.invalid', token_file='unused').deliver('keeper', {})


@pytest.mark.parametrize('rc,status', [(0, 'completed'), (7, 'failed'), (-9, 'failed'), (124, 'timeout')])
def test_terminal_delivery_once(tmp_path, rc, status):
    service = spawn_service(state=tmp_path, keeper_sink=FileInbox(tmp_path))
    record = dict(run_id='run-test', status=status, exit_code=rc, started=1, ended=2)
    service._save(record)
    service._save(record)
    for recipient in ('parent', 'keeper'):
        events = [json.loads(line) for line in (tmp_path / 'inbox' / (recipient + '.jsonl')).read_text().splitlines()]
        assert len(events) == 1
        assert events[0]['run_id'] == 'run-test'
        assert events[0]['rc'] == rc
        assert events[0]['event'] == ('DONE' if rc == 0 else 'FAILED')
        assert events[0]['head_sha']
        assert events[0]['take_cmd'] and events[0]['discard_cmd']


def test_wait_on_record(tmp_path):
    service = spawn_service(state=tmp_path)
    record = dict(run_id='run-test', status='running', started=1, ended=None)
    service._save(record)

    async def run():
        async def finish():
            await asyncio.sleep(.03)
            record.update(status='completed', exit_code=0, ended=2)
            service._save(record)
        task = asyncio.create_task(finish())
        result = await service.wait('run-test', timeout=1)
        await task
        assert result['exit_code'] == 0
    asyncio.run(run())


def test_wait_timeout_does_not_complete_run(tmp_path):
    service = spawn_service(state=tmp_path)
    service._save(dict(run_id='run-test', status='running', started=1, ended=None))
    with pytest.raises(TimeoutError):
        asyncio.run(service.wait('run-test', timeout=.01))
    assert service.result('run-test')['status'] == 'running'


def test_command_hook_and_injected_sink(tmp_path, monkeypatch):
    from plangraph.spawner.notify import CommandSink
    calls = []
    monkeypatch.setenv('PLANGRAPH_SPAWN_NOTIFY_CMD', 'fleet-notify --json')
    monkeypatch.setattr(CommandSink, 'deliver', lambda self, recipient, event: calls.append((recipient, event)))
    class Sink:
        def __init__(self):
            self.events = []
        def deliver(self, recipient, event):
            self.events.append((recipient, event))
    sink = Sink()
    service = spawn_service(state=tmp_path, completion_sink=sink, keeper_sink=sink)
    record = dict(run_id='run-hook', status='failed', exit_code=9, started=1, ended=2)
    service._save(record)
    service._save(record)
    assert [recipient for recipient, _ in calls] == ['parent', 'keeper']
    assert [recipient for recipient, _ in sink.events] == ['parent', 'keeper']
    assert calls[0][1]['event_id'] == calls[1][1]['event_id']


def test_inbox_concurrent_supervisors(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from plangraph.spawner.notify import FileInbox
    sink = FileInbox(tmp_path)
    events = [dict(event_id=f'run-{i}:completion') for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda event: sink.deliver('parent', event), events * 2))
    lines = (tmp_path / 'inbox' / 'parent.jsonl').read_text().splitlines()
    assert len(lines) == 20
    assert {json.loads(line)['event_id'] for line in lines} == {event['event_id'] for event in events}


@pytest.mark.parametrize('script,rc', [('raise SystemExit(0)', 0), ('raise SystemExit(7)', 7),
    ('import os,signal; os.kill(os.getpid(), signal.SIGKILL)', -9),
    ('import time; time.sleep(60)', 124)])
def test_real_child_exit(tmp_path, monkeypatch, script, rc):
    from plangraph.spawner.runner import SpawnerRunner
    if rc == 124:
        monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '.03')
    monkeypatch.setattr('plangraph.spawner.runner.build_command', lambda **kw: [sys.executable, '-c', script])
    runner = SpawnerRunner()
    service = spawn_service(runner=runner, state=tmp_path, keeper_sink=FileInbox(tmp_path))
    async def run():
        job = await runner.spawn('test', model_or_combo='omniroute/cx-gpt-6.2-sol-medium')
        await runner._tasks[job.job_id]
        assert job.exit_code == rc
        record = dict(run_id='run-child', status=job.status, exit_code=job.exit_code, started=1, ended=job.end_time)
        service._save(record)
        service._save(record)
        for recipient in ('parent', 'keeper'):
            events = (tmp_path / 'inbox' / (recipient + '.jsonl')).read_text().splitlines()
            assert len(events) == 1
            assert json.loads(events[0])['rc'] == rc
    asyncio.run(run())


class _Refusing:
    """A keeper sink double that answers every delivery with one HTTP status."""

    def __init__(self, status):
        self.status = status
        self.calls = 0

    def deliver(self, recipient, event):
        from plangraph.spawner.notify import KeeperRefused
        self.calls += 1
        raise KeeperRefused(self.status)


def _terminal(service, run_id='run-refused'):
    record = dict(run_id=run_id, status='completed', exit_code=0, started=1, ended=2, item='p:item')
    service._save(record)
    return service._path(run_id).parent


def test_conflict_refused_completion_is_not_sent_again(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    sink = _Refusing(409)
    service = spawn_service(state=tmp_path, keeper_sink=sink)
    folder = _terminal(service)
    assert (folder / 'keeper.delivered').read_text() == 'refused: HTTP 409\n'
    for _ in range(3):
        service.retry_completions()
    assert sink.calls == 1


def test_same_other_refusal_twice_is_final_and_transient_refusals_retry(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    sink = _Refusing(422)
    service = spawn_service(state=tmp_path / 'a', keeper_sink=sink)
    folder = _terminal(service)
    assert not (folder / 'keeper.delivered').exists()
    service.retry_completions()
    assert (folder / 'keeper.delivered').read_text() == 'refused: HTTP 422\n'
    service.retry_completions()
    assert sink.calls == 2
    auth = _Refusing(403)
    service = spawn_service(state=tmp_path / 'b', keeper_sink=auth)
    folder = _terminal(service)
    for _ in range(3):
        service.retry_completions()
    assert auth.calls == 4 and not (folder / 'keeper.delivered').exists()


def test_keeper_sink_maps_4xx_to_a_refusal(monkeypatch, tmp_path):
    import io
    import urllib.error
    from plangraph.spawner.notify import KeeperRefused, KeeperSink
    token = tmp_path / 'token'
    token.write_text('t')
    sink = KeeperSink(url='https://keeper.invalid', token_file=str(token))

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 409, 'conflict', {}, io.BytesIO(b''))

    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    with pytest.raises(KeeperRefused) as exc:
        sink.deliver('keeper', {'item': 'p:i', 'run_id': 'r', 'rc': 0})
    assert exc.value.status == 409
