import asyncio
import hashlib
import json
import stat
import inspect
import ast
from collections import Counter
from pathlib import Path

import pytest

from plangraph.spawner import runner


def test_temporary_blocks_are_tagged():
    source = inspect.getsource(runner.SpawnerRunner.spawn)
    for block in ('if prepare_launch and not dry_run:',
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


def test_run_call_sites_require_prompt_record(root=None):
    root = root or Path(runner.__file__).parent
    allow = {
        ('leg_probe.py', 'launch', "runner.spawn(prompt=PROMPT, role='implement', model_or_combo=leg.model, client=leg.harness, cwd=root, extra_env=CHILD_ISOLATION, watchdog=dict(stall_s=timeout + 60, first_edit_s=timeout + 60, max_s=timeout + 60, interval=1))"): 'Liveness probe has no run record.',
        ('launch_spec.py', 'build', "service.runner.spawn(prompt=prompt, model_or_combo=leg.model, role=request['role'], dry_run=True, client=leg.harness, extra_env=writer_env, prepare_launch=True)"): 'Dry preparation only; Rust records the actual run before dispatch.',
        ('mcp.py', 'call_tool', "self.service.spawn(card=arguments.get('card'), prompt=arguments['prompt'], role=arguments.get('role', 'implement'), model=arguments.get('model_or_combo'), override_reason=arguments.get('override_reason'), review_of=arguments.get('review_of'), writer_families=arguments.get('writer_families', ()), review_scope=arguments.get('review_scope'), seats=arguments.get('seats', False), dry_run=arguments.get('dry_run', False), wait_seconds=arguments.get('wait_seconds', 0), no_fallthrough=arguments.get('no_fallthrough', False), **options)"): 'Service entry point creates the run record.',
        ('cli.py', 'run', 'service.spawn(**payload, wait_seconds=0)'): 'Service entry point creates the run record.',
        ('cli.py', 'main', 'service.spawn(**payload, wait_seconds=None)'): 'Service entry point creates the run record.',
        ('review_auto.py', 'reviews', "service.spawn(card=card, prompt=prompt, role='review', writer_families=writers, _plan=replace(plan, legs=(leg,), review_seats=(leg,), error=None), no_fallthrough=True)"): 'Service entry point creates the review prompt/run record, as for CLI spawn.',
    }
    check_run_call_sites(root, Counter({site: 1 for site in allow}))


def check_run_call_sites(root, allow):
    seen = Counter()
    for path in root.glob('*.py'):
        source = path.read_text()
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == 'spawn'):
                continue
            owner = parents[call]
            while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents[owner]
            site = (path.name, owner.name, ' '.join(ast.get_source_segment(source, call).split()))
            if site in allow:
                seen[site] += 1
                assert seen[site] <= allow[site], ('extra allowlisted call', site)
            else:
                assert any(k.arg == 'prompt_record' and not (isinstance(k.value, ast.Constant)
                           and k.value.value is None) for k in call.keywords), site
    assert seen == allow, ('stale allowlist', allow - seen)


def test_prompt_record(tmp_path, monkeypatch):
    received = []
    monkeypatch.setattr(runner, 'build_command', lambda **k: (received.append(k['prompt']),
        (_ for _ in ()).throw(RuntimeError('dispatch')))[1])
    secret = 'sk' + '-' + 'a' * 20
    path, record = tmp_path / 'prompt.txt', {}
    saves = []
    with pytest.raises(RuntimeError, match='dispatch'):
        asyncio.run(runner.SpawnerRunner().spawn(
            prompt='héllo ' + secret, role='review',
            prompt_record=(path, record, lambda r: saves.append(dict(r)))))
    assert path.read_bytes() == b'h\xc3\xa9llo [REDACTED]'
    assert path.read_bytes() == received[0].encode()
    assert secret not in received[0]
    assert secret not in json.dumps(record)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert record == dict(prompt_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), wake_variant='initial')
    assert saves == [record]


def test_launch_preparation_redacts_once(monkeypatch):
    calls = []
    original = runner.redact
    monkeypatch.setattr(runner, 'redact', lambda p: (calls.append(p), original(p))[1])
    prompt = 'sk' + '-' + 'a' * 20
    job = asyncio.run(runner.SpawnerRunner().spawn(
        prompt=prompt, role='review', dry_run=True, prepare_launch=True))
    assert job.task_prompt == '[REDACTED]'
    assert calls == [prompt]
    with pytest.raises(ValueError, match='requires dry run'):
        asyncio.run(runner.SpawnerRunner().spawn(prompt=prompt, prepare_launch=True))


def test_write_failure_prevents_dispatch(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    monkeypatch.setattr(private_io, 'atomic_write_private', lambda *a: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(runner, 'build_command', lambda **k: pytest.fail('dispatched'))
    with pytest.raises(OSError):
        asyncio.run(runner.SpawnerRunner().spawn(prompt='p',
            prompt_record=(tmp_path / 'prompt.txt', {}, lambda r: None)))


@pytest.mark.parametrize('text', ['risk-tagged', 'task-contract'])
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
    path, record = tmp_path / 'prompt.txt', {}
    asyncio.run(runner.SpawnerRunner().spawn(prompt='hello', role='review', dry_run=True,
        prompt_record=(path, record, lambda r: pytest.fail('dry save'))))
    assert calls == ['hello']
    assert commands == ['hello']
    assert record == dict(prompt_sha256=hashlib.sha256(b'hello').hexdigest(), wake_variant='initial')
    assert not path.exists()


def test_atomic_failure_preserves_previous_prompt(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    path = tmp_path / 'prompt.txt'
    path.write_text('previous')
    monkeypatch.setattr(private_io.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        asyncio.run(runner.SpawnerRunner().spawn(prompt='next',
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
        kwargs.update(role='review')
        instance = runner.SpawnerRunner()
        monkeypatch.setattr(runner, 'build_command', lambda **k: (_ for _ in ()).throw(RuntimeError('dispatch')))
        with pytest.raises(RuntimeError, match='dispatch'):
            await instance.spawn(**kwargs)
        assert kwargs['prompt_record'][1]['wake_variant'] == 'fixer'
        return SimpleNamespace(job_id='job-test', status='completed', exit_code=0, stdout='')
    monkeypatch.setattr(service.runner, 'spawn', spawn)
    record = dict(run_id='run-parent', role='implement', exit_code=0, cwd=str(tmp_path),
                  chosen_leg=dict(model='auto', harness='opencode'))
    asyncio.run(service._continue_writer(record, 'gaps'))
    child = service._path(record['continuation_run_id']).with_name('prompt.txt')
    assert child.exists()


@pytest.mark.parametrize('calls, allowed', [(2, 1), (0, 1), (1, 2)])
def test_guard_counts_exact_sites(tmp_path, calls, allowed):
    call = 'service.spawn(prompt="p")'
    (tmp_path / 'probe.py').write_text('def probe():\n    pass\n' +
        ''.join(f'    {call}\n' for _ in range(calls)))
    allow = Counter({('probe.py', 'probe', call): allowed})
    with pytest.raises(AssertionError, match=(
            'extra allowlisted call' if calls > allowed else 'stale allowlist')):
        check_run_call_sites(tmp_path, allow)
