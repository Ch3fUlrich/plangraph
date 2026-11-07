import asyncio
import json
import io
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.planner.routing import SpawnRefused
from plangraph.spawner.prompt_guard import admit_prompt
from plangraph.spawner.service import SpawnService
from sessions_support import site, make, go, FakeRun


@pytest.mark.parametrize('code', [0x00, 0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x7F, 0x85])
@pytest.mark.parametrize('newline', ['\n', '\r', '\r\n'])
def test_helper_refuses(code, newline):
    with pytest.raises(SpawnRefused) as caught:
        admit_prompt('private' + newline + chr(code) + '\x00', SpawnRefused)
    assert str(caught.value) == f'prompt contains control U+{code:04X} on line 2'


@pytest.mark.parametrize('text', ['\t\n\r\r\n', r'\x0b', 'é文\u00a0', '', None, 42])
def test_helper_passes(text):
    assert admit_prompt(text, SpawnRefused) == text


@pytest.mark.parametrize('hardened', [False, True])
@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('fixer', [0, 1, 2])
def test_service(tmp_path, monkeypatch, hardened, dry_run, fixer):
    from plangraph.spawner.runner import spawn_service
    monkeypatch.setenv('PLANGRAPH_HOST', 'test')
    runner = SimpleNamespace(jobs={})
    service = spawn_service(runner=runner, state=tmp_path) if hardened else SpawnService(runner, tmp_path)
    calls = []
    async def dispatch(card=None, prompt='', role='implement', dry_run=False):
        calls.append(prompt)
        return 'launched'
    monkeypatch.setattr(service, '_spawn', dispatch)
    card = {'findings': [{'text': '\x0b'}]} if fixer else {}
    if fixer == 2:
        monkeypatch.setattr('plangraph.spawner.fixer.split_findings',
                            lambda *a, **kw: [[{'text': 'clean'}], [{'text': '\x0b'}]])
    with pytest.raises(SpawnRefused, match='U\\+000B'):
        asyncio.run(service.spawn(card=card, prompt='clean' if fixer else '\x0b', dry_run=dry_run))
    assert calls == []
    records = list((tmp_path / 'runs').glob('*/record.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert (record['status'], record['refusal'], record['exit_code']) == ('refused', 'SpawnRefused', 2)
    assert asyncio.run(service.spawn(prompt='\x0b', role='review')) == 'launched'


def test_launch_spec(tmp_path, monkeypatch):
    from plangraph.spawner import launch_spec as spec
    req = dict(run_id='run-' + 'a' * 32, job_id='test', role='implement', dry_run=True,
               leg={'id': 'mimo-v2.6-flash'}, prompt='\x0c', card={},
               cwd=str(tmp_path), state=str(tmp_path / 'state'))
    with pytest.raises(spec.LaunchRefused, match='U\\+000C') as caught:
        spec.build(req)
    assert caught.value.kind == 'refused'
    # Stop at the context boundary: repository bytes must reach it unchanged.
    class ContextReached(Exception):
        pass
    def annotate(record, prompt, card, cwd):
        assert prompt == 'clean' and card['context'] == '\x0c'
        raise ContextReached
    monkeypatch.setattr(spec.context_pack, 'annotate', annotate)
    monkeypatch.setattr('plangraph.spawner.target.admit_leg', lambda leg: None)
    req.update(prompt='clean', card={'context': '\x0c'})
    with pytest.raises(ContextReached):
        spec.build(req)


def test_launch_spec_cli(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import cli
    calls = []
    def no_process(*args, **kwargs):
        calls.append(args)
        raise AssertionError('process launch forbidden')
    monkeypatch.setattr(subprocess, 'Popen', no_process)
    monkeypatch.setattr(cli, '_start_supervisor', no_process)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    req = dict(run_id='run-' + 'b' * 32, job_id='test', role='implement', dry_run=False,
               leg={'id': 'mimo-v2.6-flash'}, prompt='private-prompt\x0b', card={},
               cwd=str(tmp_path), state=str(tmp_path / 'state'))
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(req).encode('utf-8'))))
    assert cli.main(['--state', req['state'], 'launch-spec']) == 2
    captured = capsys.readouterr()
    assert 'U+000B' in captured.err and 'private-prompt' not in captured.err
    assert captured.out == '' and calls == []
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('explicit', [False, True])
def test_none_role_refused(tmp_path, monkeypatch, explicit):
    from plangraph.spawner import cli, launch_spec
    calls = []
    def no_process(*args, **kwargs):
        calls.append(args)
        raise AssertionError('process launch forbidden')
    monkeypatch.setattr(subprocess, 'Popen', no_process)
    monkeypatch.setattr(cli, '_start_supervisor', no_process)
    service = SpawnService(SimpleNamespace(jobs={}, spawn=no_process), tmp_path)
    options = {'leg': 'mimo-v2.6-flash'} if explicit else {}
    with pytest.raises(ValueError, match='role must be implement'):
        asyncio.run(service.spawn(card={'estimate_tokens': 1}, prompt='\x0b', role=None, **options))
    records = list((tmp_path / 'runs').glob('*/record.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['role'] is None and record['status'] == 'failed'
    req = dict(run_id='run-' + 'c' * 32, job_id='test', role=None, dry_run=False,
               leg={'id': 'mimo-v2.6-flash'}, prompt='\x0b', card={},
               cwd=str(tmp_path), state=str(tmp_path / 'spec-state'))
    with pytest.raises(launch_spec.InvalidRequest, match='^role$'):
        launch_spec.build(req)
    assert not (tmp_path / 'spec-state').exists()
    assert calls == []


@pytest.mark.parametrize('file', [False, True])
def test_session_dry_run(site, file):
    from plangraph.routing_errors import ParameterRefused
    run = FakeRun()
    session = make(site, run, platform='win32', which=lambda _: 'claude.exe')
    args = {'prompt': 'private\n\x85'}
    if file:
        path = site.tmp_path / 'brief.txt'
        path.write_text(args['prompt'], encoding='utf-8')
        args = {'prompt': None, 'prompt_file': str(path)}
    with pytest.raises(ParameterRefused, match='U\\+0085.*line 2'):
        go(site, session, dry_run=True, **args)
    assert run.calls == []
