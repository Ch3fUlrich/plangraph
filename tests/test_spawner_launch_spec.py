import io
import json
import os
import time

import pytest

from plangraph.spawner import launch_spec as spec


@pytest.fixture
def request_data(tmp_path, monkeypatch):
    from plangraph.spawner import runner, opencode_overlay
    from plangraph.routing_support import host_keys, opencode_render
    monkeypatch.setattr(opencode_overlay, 'overlay_env', lambda env: env)
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda *a: None)
    monkeypatch.setattr(host_keys, 'add_host_keys', lambda *a, **kw: None)
    async def available(*a, **kw):
        return None
    monkeypatch.setattr(spec, 'host_skip_reason_async', available)
    monkeypatch.setattr(spec, 'admitted_by_render', lambda model: True)
    base = dict(PATH=str(tmp_path), SystemRoot='system', COMSPEC='cmd', TEMP=str(tmp_path))
    monkeypatch.setattr(runner, 'build_child_env', lambda *a, **kw: dict(base, **(a[0] or {})))
    return dict(run_id='run-' + 'a' * 32, job_id='external-job', role='implement',
                dry_run=False, leg={'id': 'qodercli-qwen3.8-flash'}, prompt='do work',
                card={}, cwd=str(tmp_path), state=str(tmp_path / 'state'))


def invoke(req):
    out, err = io.StringIO(), io.StringIO()
    raw = json.dumps(req).encode() if not isinstance(req, bytes) else req
    code = spec.main(io.BytesIO(raw), out, err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize('key', ['run_id', 'job_id', 'role', 'dry_run', 'leg', 'prompt', 'card', 'cwd', 'state'])
@pytest.mark.parametrize('mode', ['missing', 'type'])
def test_keys(request_data, key, mode):
    if mode == 'missing':
        del request_data[key]
    else:
        request_data[key] = None
    assert invoke(request_data) == (1, '', f'launch-spec failed: invalid request ({key})\n')


@pytest.mark.parametrize('key,value', [('extra', 1), ('run_id', 'run-ABC'), ('job_id', '!'),
    ('role', 'fix'), ('dry_run', 1), ('leg', {'id': ''}), ('cwd', '.'), ('state', '.')])
def test_values(request_data, key, value):
    request_data[key] = value
    assert invoke(request_data) == (1, '', f'launch-spec failed: invalid request ({key})\n')


@pytest.mark.parametrize('raw', [b'?', b'[]', b'\xff', b'{} {}', b' ' * (16 * 1024 * 1024 + 1)])
def test_json(raw):
    assert invoke(raw) == (1, '', 'launch-spec failed: invalid request (json)\n')


def test_unknown(request_data):
    request_data['leg'] = {'id': 'unknown'}
    assert invoke(request_data) == (2, '', 'unknown_leg: unknown leg id\n')


@pytest.mark.parametrize('role', ['implement', 'review'])
def test_tools(request_data, role, tmp_path):
    from plangraph.spawner.runner import IMPLEMENT_HEADER
    exe = tmp_path / 'qodercli'
    exe.write_text('#!/bin/sh\nexit 99\n')
    exe.chmod(0o700)
    request_data.update(role=role, leg={'id': request_data['leg']['id'], 'model': 'untrusted'})
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    result = json.loads(out)
    assert result['argv'][0] == str(exe)
    assert result['stdin'] is None and result['cwd'] == str(tmp_path)
    assert ('GIT_AUTHOR_NAME' in result['env']) == (role == 'implement')
    assert ('GIT_AUTHOR_EMAIL' in result['env']) == (role == 'implement')
    assert (IMPLEMENT_HEADER in ' '.join(result['argv'])) == (role == 'implement')
    assert ('End every commit message' in ' '.join(result['argv'])) == (role == 'implement')
    assert result['events'][-2]['event'] == 'admitted_by_render'


def test_opencode(request_data):
    request_data.update(role='review', leg={'id': 'mimo-v2.6-flash'})
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    result = json.loads(out)
    job = result['events'][-1]['python_job_id']
    assert result['events'][-1]['event'] == 'launch_spec'
    assert result['argv'][result['argv'].index('--title') + 1] == 'plangraph-' + job
    assert result['stdin'] == request_data['prompt']
    prefix = request_data['state'] + '/launch/' + request_data['run_id'] + '/'
    assert all(result['env'][key].startswith(prefix) for key in ('HOME', 'XDG_CONFIG_HOME'))


def test_dry(request_data, monkeypatch):
    monkeypatch.setattr(spec, 'host_skip_reason_async', lambda *a, **kw: pytest.fail('host check'))
    request_data['dry_run'] = True
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    assert json.loads(out)['argv'][0] == 'qodercli'


@pytest.mark.parametrize('kind', ['render_refused', 'unavailable', 'launch', 'inline_diff_failed', 'context', 'refused'])
def test_refusals(request_data, monkeypatch, kind):
    from plangraph.routing_support import opencode_render
    from plangraph.spawner.inline_diff import InlineDiffError
    if kind == 'render_refused':
        request_data['leg'] = {'id': 'mimo-v2.6-flash'}
        monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda *a: 'blocked')
    elif kind == 'unavailable':
        async def unavailable(*a, **kw):
            return 'blocked ' + 'sk-' + 'secret' * 8 + '\nnext'
        monkeypatch.setattr(spec, 'host_skip_reason_async', unavailable)
    elif kind == 'launch':
        monkeypatch.setattr(spec.runner, '_is_nt', lambda: True)
        request_data['prompt'] = 'x' * 40000
    elif kind == 'inline_diff_failed':
        def fail(*a):
            raise InlineDiffError('blocked')
        monkeypatch.setattr(spec.SpawnService, '_leg_prompt', fail)
    elif kind == 'context':
        request_data['card'] = {'context': True}
    else:
        async def fail(*a, **kw):
            raise ValueError('blocked')
        monkeypatch.setattr(spec.runner.SpawnerRunner, 'spawn', fail)
    code, out, err = invoke(request_data)
    assert code == 2 and out == '' and err.startswith(kind + ': ')
    assert err.count('\n') == 1 and len(err.rstrip('\n')) <= 300
    if kind == 'unavailable':
        assert '[REDACTED]' in err and 'secret' not in err
    if kind == 'launch':
        assert 'command_line_too_long' in err


def test_exception(request_data, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError('sk-' + 'private' * 8)
    monkeypatch.setattr(spec.context_pack, 'annotate', fail)
    assert invoke(request_data) == (1, '', 'launch-spec failed: RuntimeError\n')


def test_windows(request_data, monkeypatch):
    monkeypatch.setattr(spec.runner, '_is_nt', lambda: True)
    assert invoke(request_data) == (1, '', 'launch-spec failed: windows env incomplete (PATHEXT)\n')


def test_sweep(request_data):
    from pathlib import Path
    root = Path(request_data['state'])
    paths = []
    for i, status in enumerate(['completed', 'running', 'queued', None, None]):
        name = 'run-' + str(i) * 32
        path = root / 'launch' / name
        path.mkdir(parents=True)
        paths.append(path)
        if status:
            record = root / 'runs' / name / 'record.json'
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps(dict(status=status)))
    os.utime(paths[-1], (time.time() - 90000,) * 2)
    assert invoke(request_data)[0] == 0
    assert [p.exists() for p in paths] == [False, True, True, True, False]


def test_secret(request_data, monkeypatch, tmp_path):
    secret = 'sk-' + 'private' * 8
    original = spec.runner.build_child_env
    monkeypatch.setattr(spec.runner, 'build_child_env', lambda *a, **kw: dict(original(*a, **kw), TEST_KEY=secret))
    request_data['leg'] = {'id': 'mimo-v2.6-flash'}
    code, out, err = invoke(request_data)
    assert code == 0 and json.loads(out)['env']['TEST_KEY'] == secret and secret not in err
    assert all(secret.encode() not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())


@pytest.mark.parametrize('unknown,expected', [(False, 0), (True, 2)])
def test_cli(request_data, monkeypatch, unknown, expected):
    from plangraph.spawner import cli
    if unknown:
        request_data['leg'] = {'id': 'unknown'}
    monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request_data).encode())))
    monkeypatch.setattr(cli.sys, 'stdout', io.StringIO())
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: pytest.fail('constructed before dispatch'))
    assert cli.main(['--state', 'ignored', 'launch-spec']) == expected


def test_events_warnings_and_current(request_data, monkeypatch):
    from pathlib import Path
    def annotate(rec, *args):
        rec['warnings'] = ['warning', 123]
        rec['events'].append({'event': 'context'})
    monkeypatch.setattr(spec.context_pack, 'annotate', annotate)
    record = Path(request_data['state']) / 'runs' / request_data['run_id'] / 'record.json'
    record.parent.mkdir(parents=True)
    record.write_text('{"status":"completed"}')
    result = spec.build(request_data)
    assert result['warnings'] == ['warning'] and result['events'][0] == {'event': 'context'}
    current = Path(request_data['state']) / 'launch' / request_data['run_id']
    assert current.is_dir() and current.stat().st_mode & 0o777 == 0o700


def test_refusal_limit(request_data, monkeypatch):
    async def unavailable(*a, **kw):
        return 'blocked\r\n' + 'x' * 400
    monkeypatch.setattr(spec, 'host_skip_reason_async', unavailable)
    code, out, err = invoke(request_data)
    assert code == 2 and out == '' and len(err) == 301 and err.count('\n') == 1
