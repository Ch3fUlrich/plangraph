"""Hermetic output-boundary regression tests; credentials are synthetic parts."""
import asyncio
import io
import json
import os
import time
from types import SimpleNamespace

import pytest

from plangraph.spawner import env_redact, runner as runner_mod, service, run_watchdog
from plangraph.spawner.run_watchdog import collect
from plangraph.spawner.service import SpawnService


def sample():
    password = '-'.join(('fake', 'writer', 'password'))
    return password, 'postgres' + '://writer:' + password + '@example.invalid/db'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # Replace only module lookup sites; plugins keep the real os.environ.
    for module in (runner_mod, service, run_watchdog):
        monkeypatch.setattr(module, 'os', SimpleNamespace(environ={}))


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


@pytest.mark.parametrize('password', ['ab/cd', 'p@ss', 'ab:cd/@ef'])
def test_reserved_dsn_password(password):
    assert env_redact.redact('db://u:' + password + '@h/x') == 'db://u:[REDACTED]@h/x'


def test_url_port_with_path_at_fails_closed():
    assert env_redact.redact('https://host:8080/a@b') == 'https://host:[REDACTED]@b'


@pytest.mark.parametrize('text', ['https://host:8080/a', 'db://u:ab/cd', '"db://u:ab/cd"'])
def test_url_without_at_is_byte_identical(text):
    assert env_redact.redact(text).encode() == text.encode()


@pytest.mark.parametrize('quote', ['"', "'"])
@pytest.mark.parametrize('separator', [': ', '='])
def test_quoted_secret_preserves_structure(quote, separator):
    text = quote + 'password' + quote + separator + quote + 'fake\\' + quote + 'value' + quote
    expected = quote + 'password' + quote + separator + quote + '[REDACTED]' + quote
    assert env_redact.redact(text) == expected


@pytest.mark.parametrize('payload', ['dsn', 'reserved_dsn', 'json', 'escaped_json'])
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('failure', [False, True])
def test_real_output_boundaries(tmp_path, monkeypatch, split, failure, payload):
    password, dsn = sample()
    if payload == 'reserved_dsn':
        password = 'ab/cd@ef:gh'
        dsn = 'db://writer:' + password + '@example.invalid/db'
    elif payload in ('json', 'escaped_json'):
        password = 'fake-value' if payload == 'json' else 'fake\\"value'
        dsn = '{"password": "' + password + '"}'
    text = 'diagnostic ' + dsn + '\n'
    if failure:
        original_sub = env_redact.re.sub
        def broken(pattern, *args, **kwargs):
            if pattern.startswith(r'(?i)Bearer'):
                raise RuntimeError('synthetic failure')
            return original_sub(pattern, *args, **kwargs)
        monkeypatch.setattr(env_redact, 're', SimpleNamespace(
            **{name: getattr(env_redact.re, name) for name in dir(env_redact.re)
               if name != 'sub'}, sub=broken))

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
        assert password not in log
        if failure:
            assert 'redaction failed; output withheld' in log
        elif payload in ('dsn', 'reserved_dsn'):
            assert '@example.invalid/db' in log
        else:
            assert '{"password": "[REDACTED]"}' in log

        service = SpawnService(runner=runner, state=tmp_path)
        record = dict(run_id='run-test', host='synthetic', stdout_tail=text,
                      stderr_tail=text, status='running', job_id='j')
        service._save(record)
        stored = service._path('run-test').read_text()
        assert password not in stored
        marker = 'redaction failed; output withheld' if failure else '[REDACTED]'
        assert marker in json.loads(stored)['stdout_tail']

        path = tmp_path / 'prompt.txt'
        def stop_after_capture(record):
            raise RuntimeError('captured')
        with pytest.raises(RuntimeError, match='captured'):
            await object.__new__(runner_mod.SpawnerRunner).spawn(prompt=text,
                role='review', prompt_record=(path, {}, stop_after_capture))
        assert password not in path.read_text()
        assert marker in path.read_text()

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
        assert marker.encode() in stdin.getvalue()

    asyncio.run(run())


def test_environment_lookup_is_local():
    assert runner_mod.os is not os
    assert service.os is not os
    assert run_watchdog.os is not os


def test_single_scrub_implementation():
    assert runner_mod.redact is env_redact.redact


@pytest.mark.parametrize('wake_variant', ['resume', 'retry', 'steer'])
def test_replayed_prompt_capture(tmp_path, wake_variant):
    password, dsn = sample()
    path = tmp_path / 'prompt.txt'
    runner = object.__new__(runner_mod.SpawnerRunner)

    def stop_after_capture(record):
        raise RuntimeError('captured')

    with pytest.raises(RuntimeError, match='captured'):
        asyncio.run(runner.spawn(prompt='earlier output:\n' + dsn, role='review',
            wake_variant=wake_variant, prompt_record=(path, {}, stop_after_capture)))
    assert password not in path.read_text()
    assert '[REDACTED]' in path.read_text()


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


@pytest.mark.parametrize('scrub', [env_redact.redact, env_redact.redact_source, env_redact.redact_output])
def test_token_scrub_failure_withholds_input(monkeypatch, scrub):
    calls = []
    def broken(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('synthetic token scrub failure')

    # Patch the lookup site of the imported redact_tokens.TOKENS reference.
    monkeypatch.setattr(env_redact, 'TOKENS', SimpleNamespace(sub=broken))
    text = 'diagnostic ' + 'gh' + 'p_' + 'A9' * 15 + '\n'
    assert scrub(text) == '[autoos:redaction failed; output withheld]\n'
    assert calls == [True]
