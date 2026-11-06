"""Hermetic output-boundary regression tests; credentials are synthetic parts."""
import asyncio
import io
import json
import time
from types import SimpleNamespace

import pytest

from plangraph.spawner import env_redact, runner as runner_mod
from plangraph.spawner.run_watchdog import collect
from plangraph.spawner.service import SpawnService


def sample():
    password = '-'.join(('fake', 'writer', 'password'))
    return password, 'postgres' + '://writer:' + password + '@example.invalid/db'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # Replace, never inspect, the process environment. No config or live child.
    monkeypatch.setattr(runner_mod.os, 'environ', {})


@pytest.mark.parametrize('key', ['PASSWORD', 'PG_DSN', 'TOKEN', 'API_KEY', 'SECRET', 'OTHER_KEY'])
@pytest.mark.parametrize('separator', ['=', ': '])
def test_secret_keys(key, separator):
    password, _ = sample()
    text = key.lower() + separator + password + '\n'
    assert password not in runner_mod.redact(text)
    assert runner_mod.redact(text).endswith('\n')


@pytest.mark.parametrize('text', [
    'postgres://writer@example.invalid/db\n', 'password is a plain word\n',
    '0123456789abcdef\n', 'test_password_roundtrip PASSED\n',
    'ordinary\r\noutput\n',
])
def test_unchanged(text):
    assert runner_mod.redact(text).encode() == text.encode()


def test_url_host_preserved():
    password, dsn = sample()
    result = runner_mod.redact(dsn)
    assert password not in result
    assert result == 'postgres://writer:[REDACTED]@example.invalid/db'


@pytest.mark.parametrize('split', [False, True])
def test_real_output_boundaries(tmp_path, split):
    password, dsn = sample()
    text = 'diagnostic ' + dsn + '\n'

    async def run():
        job = SimpleNamespace(role='review', start_time=time.time(), cwd=str(tmp_path),
            last_activity_ts=None, events=[], status='running', survivors=[], stdout='',
            _live_log=tmp_path / 'live.log', _watchdog_baseline=('synthetic',),
            _watchdog_limits=dict(interval=60, max_s=60, stall_s=60, first_edit_s=60))
        runner = SimpleNamespace(jobs={'j': job}, _child_envs={'j': {}},
                                 _stdin_payloads={}, default_cwd=str(tmp_path))
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        proc = SimpleNamespace(stdout=stdout, stderr=stderr, stdin=None, returncode=0)
        task = asyncio.create_task(collect(runner, 'j', proc))
        cut = text.index(password) + 4 if split else len(text)
        stdout.feed_data(text[:cut].encode())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if split:
            assert password[:4] not in job.stdout
            assert not (tmp_path / 'live.log').exists()
        stdout.feed_data(text[cut:].encode())
        stdout.feed_eof()
        stderr.feed_eof()
        await task
        assert password not in job.stdout
        log = (tmp_path / 'live.log').read_text()
        assert password not in log and '@example.invalid/db' in log

        service = SpawnService(runner=runner, state=tmp_path)
        record = dict(run_id='run-test', host='synthetic', stdout_tail=text,
                      stderr_tail=text, status='running', job_id='j')
        service._save(record)
        stored = service._path('run-test').read_text()
        assert password not in stored
        assert '[REDACTED]' in json.loads(stored)['stdout_tail']

        # Steer uses the actual stdin write path, including earlier output.
        class Stdin(io.BytesIO):
            def is_closing(self):
                return False

            async def drain(self):
                pass

        stdin = Stdin()
        outbound = object.__new__(runner_mod.SpawnerRunner)
        outbound.jobs = {'j': SimpleNamespace(steer_messages=[], events=[])}
        outbound._processes = {'j': SimpleNamespace(stdin=stdin)}
        await outbound.steer('j', 'retry with earlier output:\n' + text)
        assert password.encode() not in stdin.getvalue()
        assert b'[REDACTED]' in stdin.getvalue()

    asyncio.run(run())


def test_scrub_failure(tmp_path, monkeypatch):
    from plangraph.spawner.run_watchdog import LiveLog
    password, dsn = sample()

    def broken(*args, **kwargs):
        raise RuntimeError('synthetic failure')

    monkeypatch.setattr(env_redact.re, 'sub', broken)
    log = LiveLog(SimpleNamespace(_live_log=tmp_path / 'live.log', events=[]))
    log.feed(runner_mod.redact(dsn + '\n'))
    log.flush()
    stored = (tmp_path / 'live.log').read_text()
    assert password not in stored
    assert 'redaction failed; output withheld' in stored


@pytest.mark.parametrize('boundary', ['record', 'provider'])
def test_independent_write_boundaries(tmp_path, boundary):
    password, dsn = sample()
    if boundary == 'record':
        service = SpawnService(runner=SimpleNamespace(_child_envs={}, jobs={}), state=tmp_path)
        service._save(dict(run_id='run-test', host='synthetic', stdout_tail=dsn))
        payload = service._path('run-test').read_text()
    else:
        class Stdin(io.BytesIO):
            def is_closing(self):
                return False

            async def drain(self):
                pass

        stdin = Stdin()
        runner = object.__new__(runner_mod.SpawnerRunner)
        runner.jobs = {'j': SimpleNamespace(steer_messages=[], events=[])}
        runner._processes = {'j': SimpleNamespace(stdin=stdin)}
        asyncio.run(runner.steer('j', dsn))
        payload = stdin.getvalue().decode()
    assert password not in payload
    assert '[REDACTED]' in payload


def test_stream_scrub_failure():
    def broken(text):
        raise RuntimeError('synthetic failure')

    lines = env_redact.OutputLines(broken)
    assert lines.feed('unfinished') == ''
    assert 'redaction failed; output withheld' in lines.feed('', final=True)
