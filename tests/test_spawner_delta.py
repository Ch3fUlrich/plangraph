from types import SimpleNamespace

from plangraph.spawner.env_redact import OutputLines, redact
from plangraph.spawner.exit_outcome import terminal_reason
from plangraph.spawner.seat_artifact import publish
import pytest


def test_cr_frames_and_bounded_tail():
    lines = OutputLines(redact)
    assert lines.feed('working\r') == 'working\r'
    assert lines.feed('TOKEN=') == ''
    assert lines.feed('synthetic-value\r') == 'TOKEN=[REDACTED]\r'
    assert lines.feed('x' * 100000)
    assert len(lines.pending) <= 4096


def test_old_review_skipped_tail_keeps_verdict():
    record = dict(role='review', status='completed', exit_code=0,
                  attempts=[dict(status='completed', stdout_tail='VERDICT: PASS\nTEST_GAMING: NO\n'),
                            dict(status='skipped')])
    terminal_reason(record)
    assert record['status'] == 'completed'


def test_jobless_publish_keeps_evidence(tmp_path):
    for name in ('review.txt', 'evidence.json'):
        (tmp_path / name).write_text('existing')
    publish(tmp_path, dict(role='review', ended=1), None)
    assert (tmp_path / 'review.txt').read_text() == 'existing'
    assert (tmp_path / 'evidence.json').read_text() == 'existing'


def test_discovery_retries(monkeypatch):
    from plangraph.routing_support.leg_state import LegAdapter
    results = iter([[], ['provider/model']])
    monkeypatch.setattr(LegAdapter, 'standalone_models', staticmethod(lambda: next(results)))
    provider = SimpleNamespace(snapshot=lambda: {'provider': SimpleNamespace(stale=False, available=True)})
    clock = [0]
    adapter = LegAdapter(None, provider, enabled=True, clock=lambda: clock[0])
    assert adapter.leg_status('provider/model')['state'] == 'limited'
    clock[0] = 61
    assert adapter.leg_status('provider/model')['state'] == 'ok'


def test_negative_confirmation_failure(monkeypatch):
    import hashlib
    from plangraph.routing_support import resolver as r
    from plangraph.routing_support.served_fresh import served_fresh
    config = '{}'
    key = (r.provider_config_digest(config), hashlib.sha256(b'[]').hexdigest())
    monkeypatch.setattr(r, '_served_cache', {key: (r.time.monotonic() + 290, frozenset({'other'}))})
    monkeypatch.setattr(r, '_probe_served_models', lambda *a, **kw: None)
    assert not served_fresh('model', frozenset({'other'}), config, {})


def test_posix_permission_fast(monkeypatch):
    from pathlib import Path
    from plangraph.routing_support import private_io
    sleeps = []
    monkeypatch.setattr(Path, 'read_text', lambda *a, **kw: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(private_io.time, 'sleep', sleeps.append)
    with pytest.raises(PermissionError):
        private_io.read_text_retry('record.json')
    assert not sleeps


def test_tool_progress_only():
    from plangraph.spawner.exit_outcome import tool_call_only
    job = SimpleNamespace(status='failed', stdout='✱ thinking\n<tool_call><function=read>text</function></tool_call>')
    assert tool_call_only(job, {})


def test_quoted_credit_stdout():
    from plangraph.spawner.env_redact import harness_limit_line
    line = ("You've reached your credit usage limit. Please upgrade your subscription plan "
            'to get more resources. Report Issue (input /feedback)')
    assert harness_limit_line(1, line, '') is None
    assert harness_limit_line(1, '', line) == line


def test_rounds_refused_raises(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner.runner import spawn_service
    from plangraph.routing_errors import SpawnRefused
    monkeypatch.setattr('plangraph.spawner.question_bridge.rounds_exhausted', lambda *a: True)
    service = spawn_service(runner=SimpleNamespace(jobs={}), state=tmp_path)
    with pytest.raises(SpawnRefused, match='question_rounds_exhausted'):
        asyncio.run(service._spawn(card={'item': 'synthetic'}))


@pytest.mark.parametrize('old_record', [False, True])
def test_jobless_retry_preserves_old_review(tmp_path, old_record):
    from plangraph.spawner.runner import spawn_service
    sink = SimpleNamespace(deliver=lambda *a: None)
    service = spawn_service(runner=SimpleNamespace(jobs={}), state=tmp_path,
                            completion_sink=sink, keeper_sink=sink)
    record = dict(run_id='run-synthetic', host='fixture', role='review', status='completed',
                  exit_code=0, ended=1, started=0, attempts=[
                      dict(leg='provider/model', status='completed', stdout_tail='VERDICT: PASS\nTEST_GAMING: NO\n')],
                  plan=dict(legs=[dict(id='provider/other')]))
    directory = service._path(record['run_id']).parent
    directory.mkdir(parents=True)
    for name in ('review.txt', 'evidence.json'):
        (directory / name).write_text('existing')
    service._save(record)
    assert record['attempts'][-1]['status'] == 'skipped'
    assert 'stdout_tail' not in record['attempts'][-1]
    assert not service.runner.jobs
    if old_record:
        record.pop('completion')
    service._save(record, retry=True)
    assert record['status'] == 'completed'
    assert record['completion']['event'] != 'FAILED'
    for name in ('review.txt', 'evidence.json'):
        assert (directory / name).read_text() == 'existing'


def test_hung_pid_start_identity(tmp_path):
    from plangraph.spawner.hung_capture import HungBoundary
    boundary = HungBoundary(SimpleNamespace(enabled=True), sample=lambda r: {},
                            identity=lambda p, i: i == 7, cancel=None, fallthrough=None)
    record = dict(status='running', started=0, pid=42, pid_start=7)
    assert 'identity' not in boundary.probe(tmp_path, record, 1000)


def test_collect_cr_live_probe(tmp_path):
    import asyncio
    import os
    import time
    from test_run_watchdog import _job
    from plangraph.spawner.run_watchdog import collect
    from plangraph.spawner.stall_probe import probe_run

    async def run():
        runner, job = _job(tmp_path, {'stall_min': 1, 'max_min': 2})
        job._live_log = tmp_path / 'runs/run-synthetic/live.log'
        proc = SimpleNamespace(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(),
                               returncode=None)
        task = asyncio.create_task(collect(runner, 'j', proc))
        try:
            for chunk in (b'working\r', b'TOKEN=', b'synthetic-value\r', b'sk-cross', b'chunk-secret\r'):
                proc.stdout.feed_data(chunk)
                await asyncio.sleep(.02)
            assert 'working' in job.stdout and 'synthetic-value' not in job.stdout
            assert 'synthetic-value' not in job._live_log.read_text()
            assert 'sk-cross' not in job._live_log.read_text()
            assert 'chunk-secret' not in job.stdout
            record = dict(run_id='run-synthetic', status='running', pid=os.getpid(),
                          started=time.time() - 10, card={'stall_min': .01})
            assert probe_run(tmp_path, record, time.time()) is None
        finally:
            proc.returncode = 0
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            await task
    asyncio.run(run())
