"""Caller-owned prompt files and the plan-only review contract."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.planner.routing import SpawnRefused
from plangraph.review.verdict import judge
from plangraph.spawner import cli


@pytest.fixture
def service(tmp_path, monkeypatch):
    class Service:
        state = tmp_path / 'state'
        refusals = []
        payloads = []

        def save_refusal(self, exc, role):
            self.refusals.append((exc, role))

        async def spawn(self, **payload):
            self.payloads.append(payload)
            return {'status': 'dry_run', 'exit_code': 0}

    instance = Service()
    monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: instance)
    monkeypatch.setattr(cli, '_start_supervisor', lambda *a, **k: pytest.fail('child started'))
    return instance


def spawn_args(*extra):
    return ['spawn', '--card', '{"id":"plan"}', '--leg', 'fake', *extra]


def test_dry_run_exact_text(tmp_path, service):
    path = tmp_path / 'prompt.txt'
    text = '  plan café\r\nnext line\n'
    path.write_bytes(text.encode())
    assert cli.main(spawn_args('--prompt-file', str(path), '--dry-run')) == 0
    assert service.payloads[0]['prompt'] == text
    assert list(tmp_path.iterdir()) == [path]


def test_large_prompt_stdin_not_argv(tmp_path, service, monkeypatch):
    path = tmp_path / 'prompt.txt'
    text = 'X' * 200_000
    path.write_text(text, encoding='utf-8')
    captured = {}

    class Input(io.StringIO):
        def close(self):
            captured['payload'] = json.loads(self.getvalue())
            super().close()

    def start(argv, **kwargs):
        captured['argv'] = argv
        return SimpleNamespace(stdin=Input(), stdout=io.StringIO(
            '{"status":"completed","exit_code":0}\n'))

    monkeypatch.setattr(cli, '_start_supervisor', start)
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    assert cli.main(spawn_args('--prompt-file', str(path))) == 0
    assert captured['payload']['prompt'] == text
    assert all(len(arg) < 4096 for arg in captured['argv'])
    assert path.read_text() == text


@pytest.mark.parametrize('both', [False, True])
def test_exclusive_inputs_save_refusal(tmp_path, service, both):
    path = tmp_path / 'prompt.txt'
    path.write_text('private content')
    extra = ['inline', '--prompt-file', str(path)] if both else []
    assert cli.main(spawn_args(*extra)) == 2
    exc, role = service.refusals[0]
    assert isinstance(exc, SpawnRefused)
    assert 'prompt_input_conflict' in str(exc) if both else 'prompt_input_missing' in str(exc)
    if both:
        assert str(path) in str(exc)
    assert 'private content' not in str(exc)
    assert not service.payloads


@pytest.mark.parametrize('kind,reason', [
    ('missing', 'prompt_file_not_regular'), ('directory', 'prompt_file_not_regular'),
    ('invalid', 'prompt_file_invalid_utf8'), ('empty', 'prompt_file_empty'),
    ('whitespace', 'prompt_file_empty'), ('unreadable', 'prompt_file_unreadable'),
])
def test_file_refusals(tmp_path, service, monkeypatch, kind, reason):
    path = tmp_path / 'prompt.txt'
    if kind == 'directory':
        path.mkdir()
    elif kind != 'missing':
        path.write_bytes({'invalid': b'PRIVATE\xff', 'empty': b'',
                          'whitespace': b' \n\t'}.get(kind, b'PRIVATE'))
    if kind == 'unreadable':
        monkeypatch.setattr(Path, 'open', lambda *a, **k: (_ for _ in ()).throw(
            PermissionError('PRIVATE')))
    assert cli.main(spawn_args('--prompt-file', str(path))) == 2
    exc, _ = service.refusals[0]
    assert isinstance(exc, SpawnRefused)
    assert reason in str(exc)
    assert str(path) in str(exc)
    assert 'PRIVATE' not in str(exc)


def test_bom_stripped(tmp_path, service):
    path = tmp_path / 'prompt.txt'
    path.write_bytes(b'\xef\xbb\xbfplan')
    assert cli.main(spawn_args('--prompt-file', str(path), '--dry-run')) == 0
    assert service.payloads[0]['prompt'] == 'plan'


def test_route_rejects_prompt_file_and_spawn_positional(service):
    with pytest.raises(SystemExit) as exc:
        cli.main(['route', '--prompt-file', 'prompt.txt'])
    assert exc.value.code == 2
    assert cli.main(spawn_args('inline plan', '--dry-run')) == 0
    assert service.payloads[0]['prompt'] == 'inline plan'


def plan_tail():
    import plangraph.review
    return Path(plangraph.review.__file__).with_name('tail_plan.md').read_text(encoding='utf-8')


def test_tail_hygiene():
    text = plan_tail()
    assert text.startswith('\n=== END OF MATERIAL.')
    assert 'test-gaming' not in text.lower()
    for word in ('Auto' + 'OS', 'Omni' + 'Route', 'omni' + 'route'):
        assert word not in text


def test_tail_verdict_contract():
    findings = '```plan-review\nF1-1 | major | correctness | plan | gap | UNVERIFIED | fix\n```'
    assert judge(findings + '\nVERDICT: REQUEST_CHANGES', 'stop').verdict == 'fix'
    assert judge('```plan-review\n```\nVERDICT: ACCEPT', 'stop').verdict == 'pass'
    assert judge('```plan-review\nVERDICT: ACCEPT\n```', 'stop').verdict is None
    assert judge(plan_tail(), 'stop').verdict is None
