import asyncio
import hashlib
import json
import stat
import inspect
import ast
from pathlib import Path

import pytest

from plangraph.spawner import runner


def test_temporary_blocks_are_tagged():
    source = inspect.getsource(runner.SpawnerRunner.spawn)
    for block in ('if prepare_launch and not dry_run:', 'if not prepare_launch:',
                  'if not prepare_launch and prompt_record is not None:'):
        assert block + '  # REMOVE-AT-R10' in source


def test_no_run_redacts_once_and_dispatches_without_file(tmp_path, monkeypatch):
    calls = []
    original = runner.redact
    monkeypatch.setattr(runner, 'redact', lambda p: (calls.append(p), original(p))[1])
    commands = []
    monkeypatch.setattr(runner, 'build_command', lambda **k: (commands.append(k['prompt']), ['fake'])[1])
    prompt = 'sk' + '-' + 'a' * 20
    job = asyncio.run(runner.SpawnerRunner().spawn(prompt=prompt, role='review', dry_run=True, cwd=str(tmp_path)))
    assert calls == [prompt]
    assert commands == [job.task_prompt] == ['[REDACTED]']
    assert not list(tmp_path.iterdir())


def test_run_call_sites_require_prompt_record():
    root = Path(runner.__file__).parent
    allow = {
        ('leg_probe.py', 'launch'): 'Liveness probe has no run record.',
        ('launch_spec.py', 'build'): 'Dry preparation only; Rust records the actual run before dispatch.',
    }
    seen = set()
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == 'spawn' and any(k.arg == 'model_or_combo' for k in call.keywords)):
                continue
            owner = parents[call]
            while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents[owner]
            site = (path.name, owner.name)
            if site in allow:
                seen.add(site)
            else:
                assert any(k.arg == 'prompt_record' and not (isinstance(k.value, ast.Constant)
                           and k.value.value is None) for k in call.keywords), site
    assert seen == set(allow)


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


def test_launch_preparation_leaves_redaction_to_rust(monkeypatch):
    monkeypatch.setattr(runner, 'redact', lambda p: pytest.fail('double redaction'))
    prompt = 'sk' + '-' + 'a' * 20
    job = asyncio.run(runner.SpawnerRunner().spawn(
        prompt=prompt, role='review', dry_run=True, prepare_launch=True))
    assert job.task_prompt == prompt
    with pytest.raises(ValueError, match='requires dry run'):
        asyncio.run(runner.SpawnerRunner().spawn(prompt=prompt, prepare_launch=True))


def test_write_failure_prevents_dispatch(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    monkeypatch.setattr(private_io, 'atomic_write_private', lambda *a: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(runner, 'build_command', lambda **k: pytest.fail('dispatched'))
    with pytest.raises(OSError):
        asyncio.run(runner.SpawnerRunner().spawn(prompt='p', dry_run=True,
            prompt_record=(tmp_path / 'prompt.txt', {}, lambda r: None)))


@pytest.mark.parametrize('text', ['risk-tagged', 'task-contract', 'xsk-' + 'a' * 20])
def test_ordinary_text(text):
    assert runner.redact(text) == text


@pytest.mark.parametrize('text', ['sk-' + 'a' * 19, 'sk-plantedsecret', 'sk-' + 'a' * 20, 'Bearer ' + 'b' * 10, 'API_KEY=' + 'c' * 10])
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


def test_fixer_wake_variant(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner.service import SpawnService
    from plangraph.spawner import fixer
    monkeypatch.setattr(fixer, 'remaining_gaps', lambda output: ['gap'])
    service = SpawnService(state=tmp_path)
    monkeypatch.setattr(service, '_save', lambda record: None)
    async def spawn(**kwargs):
        assert kwargs['wake_variant'] == 'fixer'
        kwargs.pop('model_or_combo')
        kwargs.pop('client')
        kwargs.update(role='review', dry_run=True)
        job = await runner.SpawnerRunner().spawn(**kwargs)
        assert kwargs['prompt_record'][1]['wake_variant'] == 'fixer'
        return SimpleNamespace(job_id=job.job_id, status='completed', exit_code=0, stdout='')
    monkeypatch.setattr(service.runner, 'spawn', spawn)
    record = dict(run_id='run-parent', role='implement', exit_code=0, cwd=str(tmp_path),
                  chosen_leg=dict(model='auto', harness='opencode'))
    asyncio.run(service._continue_writer(record, 'gaps'))
    child = service._path(record['continuation_run_id']).with_name('prompt.txt')
    assert child.exists()
