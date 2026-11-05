import asyncio
import hashlib
import json
import stat

import pytest

from plangraph.spawner import runner


def test_prompt_record(tmp_path):
    secret = 'sk' + '-' + 'a' * 20
    path, record = tmp_path / 'prompt.txt', {}
    saves = []
    job = asyncio.run(runner.SpawnerRunner().spawn(
        prompt='héllo ' + secret, role='review', dry_run=True,
        prompt_record=(path, record, lambda r: saves.append(dict(r)))))
    assert path.read_bytes() == job.task_prompt.encode() == b'h\xc3\xa9llo [REDACTED]'
    assert secret not in json.dumps([record, job.to_dict()])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert record == dict(prompt_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), wake_variant='initial')
    assert saves == [record]


def test_write_failure_prevents_dispatch(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    monkeypatch.setattr(private_io, 'atomic_write_private', lambda *a: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(runner, 'build_command', lambda **k: pytest.fail('dispatched'))
    with pytest.raises(OSError):
        asyncio.run(runner.SpawnerRunner().spawn(prompt='p', dry_run=True,
            prompt_record=(tmp_path / 'prompt.txt', {}, lambda r: None)))


@pytest.mark.parametrize('text', ['risk-tagged', 'task-contract', 'sk-learn',
    'sk-' + 'a' * 19, 'xsk-' + 'a' * 20])
def test_ordinary_text(text):
    assert runner.redact(text) == text


@pytest.mark.parametrize('text', ['sk-' + 'a' * 20, 'Bearer ' + 'b' * 10, 'API_KEY=' + 'c' * 10])
def test_credentials(text):
    assert text not in runner.redact(text)


def test_single_redaction_at_command_boundary(tmp_path, monkeypatch):
    calls, commands = [], []
    original = runner.redact
    monkeypatch.setattr(runner, 'redact', lambda p: (calls.append(p), original(p))[1])
    monkeypatch.setattr(runner, 'build_command', lambda **kw: (commands.append(kw['prompt']), ['fake'])[1])
    path = tmp_path / 'prompt.txt'
    asyncio.run(runner.SpawnerRunner().spawn(prompt='hello', role='review', dry_run=True,
        prompt_record=(path, {}, lambda r: None)))
    assert calls == ['hello']
    assert commands == [path.read_text()]


def test_atomic_failure_preserves_previous_prompt(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    path = tmp_path / 'prompt.txt'
    path.write_text('previous')
    monkeypatch.setattr(private_io.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        asyncio.run(runner.SpawnerRunner().spawn(prompt='next', dry_run=True,
            prompt_record=(path, {}, lambda r: None)))
    assert path.read_text() == 'previous'
    assert list(tmp_path.iterdir()) == [path]
