import io
import json
import os
import time

import pytest

from plangraph.spawner.prompt_input import header

from plangraph.spawner import launch_spec as spec
from go_credential_fake import go_credential  # noqa: F401


@pytest.fixture
def request_data(tmp_path, monkeypatch):
    from dataclasses import replace
    from plangraph.spawner import runner, opencode_overlay
    from plangraph.routing_support import host_keys, opencode_render
    # Exercise launch mechanics with explicitly enabled historical review legs.
    monkeypatch.setattr(spec, 'DEFAULT_LEGS', tuple(
        replace(leg, disabled=None) if leg.id in ('qodercli-qwen3.8-flash', 'opencode-glm-5.3-flash-free') else leg
        for leg in spec.DEFAULT_LEGS))
    from plangraph.planner import legs
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', spec.DEFAULT_LEGS)
    monkeypatch.setattr(opencode_overlay, 'overlay_env', lambda env: env)
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda *a: None)
    monkeypatch.setattr(host_keys, 'add_host_keys', lambda *a, **kw: None)
    async def available(*a, **kw):
        return None
    monkeypatch.setattr(spec, 'host_skip_reason_async', available)
    monkeypatch.setattr(spec, 'admitted_by_render', lambda model: True)
    # The real MiMo launch gate requires an existing compatible model entry.
    base = dict(PATH=str(tmp_path), SystemRoot='system', COMSPEC='cmd', TEMP=str(tmp_path),
        OPENCODE_CONFIG_CONTENT=json.dumps({'providers': {'mimo': {
            'package': '@opencode/ai/providers/openai-compatible',
            'models': {'mimo-v2.6-flash': {'modelID': 'mimo/mimo-v2.6-flash'}}}}}))
    monkeypatch.setattr(runner, 'build_child_env', lambda *a, **kw: dict(base, **(a[0] or {})))
    return dict(run_id='run-' + 'a' * 32, job_id='external-job', role='review',
                dry_run=False, leg={'id': 'qodercli-qwen3.8-flash'}, prompt='do work',
                writer_families=['openai'], card={}, cwd=str(tmp_path), state=str(tmp_path / 'state'))


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
@pytest.mark.parametrize('explicit', [False, True])
def test_tools(request_data, role, tmp_path, explicit, monkeypatch):
    from plangraph.routing_support import clients
    monkeypatch.setattr(clients, 'resolve_codex_client', lambda env=None: (None, 'fixture'))
    leg_id = 'cx-gpt-6.2-sol' if role == 'implement' else request_data['leg']['id']
    exe = tmp_path / ('opencode' if role == 'implement' else 'qodercli')
    exe.write_text('#!/bin/sh\nexit 99\n')
    exe.chmod(0o700)
    request_data.update(role=role, leg={'id': leg_id, 'model': 'untrusted'})
    request_data['leg']['explicit'] = explicit
    if role == 'implement' and explicit:
        request_data['leg']['harness'] = 'opencode'
        monkeypatch.setattr(clients, 'resolve_codex_client', lambda env=None: ('codex', None))
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    result = json.loads(out)
    assert result['argv'][0] == str(exe)
    assert result['cwd'] == str(tmp_path)
    assert result['stdin'] is not None
    prompt = result['stdin']
    assert request_data['prompt'] not in result['argv']
    assert ('GIT_AUTHOR_NAME' in result['env']) == (role == 'implement')
    assert ('GIT_AUTHOR_EMAIL' in result['env']) == (role == 'implement')
    harness = 'opencode' if role == 'implement' else 'qodercli'
    assert prompt.startswith(header(harness, role, leg_id))
    assert ('name: ' + ('review' if role == 'review' else 'implement') in prompt)
    assert ('End every commit message' in prompt) == (role == 'implement')
    assert result['events'][-2]['event'] == 'admitted_by_render'


def test_opencode(request_data):
    request_data.update(role='review', leg={'id': 'opencode-glm-5.3-flash-free'})
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    result = json.loads(out)
    job = result['events'][-1]['python_job_id']
    assert result['events'][-1]['event'] == 'launch_spec'
    assert result['argv'][result['argv'].index('--title') + 1] == 'plangraph-' + job
    assert result['stdin'] == result['prompt'] and result['stdin'].endswith(request_data['prompt'])
    assert result['stdin'].startswith(header('opencode', 'review'))
    prefix = request_data['state'] + '/launch/' + request_data['run_id'] + '/'
    assert all(result['env'][key].startswith(prefix) for key in ('HOME', 'XDG_CONFIG_HOME'))


def test_agy_stream_spec(request_data):
    request_data.update(leg={'id': 'agy-gemini-4-neon-medium'}, prompt='é\n' + 'x' * (300 * 1024))
    result = spec.build(request_data)
    assert request_data['prompt'] not in result['argv']
    assert json.loads(result['stdin'])['message']['content'] == result['prompt'] and result['prompt'].endswith(request_data['prompt'])


def test_research_stream_spec(request_data):
    request_data.update(role='research', dry_run=True,
                        leg={'id': 'agy-gemini-4-hydrogen-high'},
                        card={'estimate_tokens': 400_000}, writer_families=[])
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    result = json.loads(out)
    assert result['argv'][0] == 'agy'
    assert json.loads(result['stdin'])['message']['content'].endswith(request_data['prompt'])
    assert 'GIT_AUTHOR_EMAIL' not in result['env']

    request_data['card']['web_required'] = True
    assert invoke(request_data) == (2, '', 'web_access_unavailable: agy-gemini-4-hydrogen-high\n')


@pytest.mark.parametrize('leg_id', ['agy-gemini-4-neon-medium', 'qodercli-qwen3.8-flash', 'opencode-glm-5.3-flash-free'])
@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('size', [16, 200 * 1024], ids=['short', '200KiB'])
def test_real_producer_prompt_stays_out_of_env(request_data, monkeypatch, leg_id, dry_run, size):
    from plangraph.routing_support.clients import build_child_env
    prompt = 'review payload\né\n' + 'x' * size + '\n\n'
    request_data.update(prompt=prompt, leg={'id': leg_id}, dry_run=dry_run)
    carriers = ('PROMPT', 'prompt', 'PrOmPt', 'PROMPT_TEXT', 'PLANGRAPH_TASK_PROMPT', 'prompt_suffix')
    for name in carriers:
        monkeypatch.setenv(name, prompt)
    settings = dict(SHELL_PROMPT_THEME='minimal', CLIENT_PROMPT_TEMPLATE_FILE='template.txt')
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('PATH', request_data['cwd'])
    # Use the real producer, not the fixture's synthetic child env. Exercise both
    # inherited and explicitly supplied legacy prompt env carriers.
    monkeypatch.setattr(spec.runner, 'build_child_env', build_child_env)
    env = build_child_env(dict.fromkeys(carriers, prompt) | settings)
    assert not set(carriers).intersection(env)
    assert {name: env[name] for name in settings} == settings
    result = spec.build(request_data)
    assert not set(carriers).intersection(result['env'])
    assert {name: result['env'][name] for name in settings} == settings
    assert all(prompt not in value for value in [*result['argv'], *result['env'].values()])
    payload = json.loads(result['stdin'])['message']['content'] if leg_id.startswith('agy-') else result['stdin']
    assert payload == result['prompt'] and payload.endswith(prompt)


@pytest.mark.parametrize('harness,leg', [('agy', 'agy-gemini-4-neon-medium'), ('qodercli', 'qodercli-qwen3.8-flash')])
@pytest.mark.parametrize('tail', [[], ['-p'], ['-p', 'private task']])
def test_transport_refusal_is_named(request_data, monkeypatch, harness, leg, tail):
    request_data['leg'] = {'id': leg}
    monkeypatch.setattr(spec.runner, 'build_command', lambda **kw: [harness, *tail])
    assert invoke(request_data) == (2, '', 'stdin_prompt_flag_missing: expected -p with an empty value\n')


def test_dry(request_data, monkeypatch):
    monkeypatch.setattr(spec, 'host_skip_reason_async', lambda *a, **kw: pytest.fail('host check'))
    request_data['dry_run'] = True
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    assert json.loads(out)['argv'][0] == 'qodercli'


@pytest.mark.parametrize('installed', [True, False])
def test_review_uses_resolved_codex_client(request_data, tmp_path, monkeypatch, installed):
    from plangraph.planner.legs import REVIEW_GPT
    from plangraph.routing_support.clients import CODEX_REVIEW_CLIENT_MISSING

    executable = tmp_path / 'resolved-codex'
    executable.symlink_to('/bin/true')
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client',
                        lambda env=None: (str(executable) if installed else None, 'fixture search'))
    request_data.update(leg={'id': REVIEW_GPT, 'harness': 'codex'},
                        writer_families=['gemini'])

    code, out, err = invoke(request_data)
    if installed:
        assert (code, err) == (0, '')
        assert json.loads(out)['argv'][0] == str(executable)
    else:
        assert (code, out, err) == (
            CODEX_REVIEW_CLIENT_MISSING, '',
            f"REVIEW {request_data['run_id']} FAIL client_missing fixture search\n")


@pytest.mark.parametrize('explicit', [None, False])
def test_inline_only_review_without_diff_refused(request_data, explicit):
    # A routed leg id alone is not an explicit document review: fail closed.
    # Only the explicit boolean opt-in may permit a missing diff_ref.
    request_data.update(role='review', leg={'id': 'mimo-v2.6-flash'})
    if explicit is not None:
        request_data['leg']['explicit'] = explicit
    code, out, err = invoke(request_data)
    assert code == 2 and out == ''
    assert 'inline_diff_failed' in err


@pytest.mark.parametrize('explicit', ['true', 1, None, [], {}])
def test_leg_explicit_invalid(request_data, explicit):
    request_data['leg']['explicit'] = explicit
    with pytest.raises(spec.InvalidRequest, match='^leg$'):
        spec.build(request_data)


@pytest.mark.parametrize('with_diff', [False, True])
def test_explicit_document_review(request_data, tmp_path, monkeypatch, with_diff):
    request_data.update(dry_run=True, leg={'id': 'mimo-v2.6-flash', 'explicit': True})
    if with_diff:
        diff = 'diff --git a/example.py b/example.py\nindex 1111111..2222222 100644\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new\n'
        from plangraph.spawner import inline_diff
        from types import SimpleNamespace
        real_run = inline_diff.subprocess.run
        # The private-key check reads each side's blob: answer it for this synthetic repo.
        monkeypatch.setattr(inline_diff.subprocess, 'run', lambda args, **kw:
            SimpleNamespace(stdout=diff, stderr='', returncode=0) if 'diff' in args else
            SimpleNamespace(stdout='plain\n', stderr='', returncode=0) if 'cat-file' in args or 'merge-base' in args
            else real_run(args, **kw))
        request_data['card']['diff_ref'] = dict(repo=str(tmp_path), base='HEAD~1', head='HEAD')
        monkeypatch.setattr('plangraph.planner.seats.head_writer_families', lambda card: [])   # tmp_path is no repository
    result = spec.build(request_data)
    service = spec.SpawnService(state=request_data['state'])
    service.runner.default_cwd = request_data['cwd']
    leg = next(leg for leg in spec.DEFAULT_LEGS if leg.id == request_data['leg']['id'])
    direct = service._leg_prompt(request_data['prompt'], request_data['card'], 'review', leg,
                                 document_review=True)
    assert result['prompt'].endswith(direct)
    if with_diff:
        assert diff in result['prompt']
    else:
        assert result['prompt'].endswith(request_data['prompt'])
        assert 'diff --git' not in result['prompt']


def test_explicit_document_review_strict_bound(request_data, monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    monkeypatch.setattr(spec, 'DEFAULT_LEGS', tuple(
        replace(leg, max_request_tokens=200) if leg.id == 'mimo-v2.6-flash' else leg
        for leg in spec.DEFAULT_LEGS))
    request_data.update(leg={'id': 'mimo-v2.6-flash', 'explicit': True}, prompt='x' * 1000)
    with pytest.raises(spec.LaunchRefused, match='inline_prompt_oversized') as exc:
        spec.build(request_data)
    assert exc.value.kind == 'inline_diff_failed'


@pytest.mark.parametrize('kind', ['render_refused', 'unavailable', 'launch', 'inline_diff_failed', 'context', 'refused'])
def test_refusals(request_data, monkeypatch, kind):
    from plangraph.routing_support import opencode_render
    from plangraph.spawner.inline_diff import InlineDiffError
    if kind == 'render_refused':
        request_data['leg'] = {'id': 'opencode-glm-5.3-flash-free'}
        monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda *a: 'blocked')
    elif kind == 'unavailable':
        async def unavailable(*a, **kw):
            return 'blocked ' + 'sk-' + 'secret' * 8 + '\nnext'
        monkeypatch.setattr(spec, 'host_skip_reason_async', unavailable)
    elif kind == 'launch':
        monkeypatch.setattr(spec.runner, '_is_nt', lambda: True)
        monkeypatch.setattr(spec.runner, '_resolve_launch', lambda *a, **kw: ('qodercli', 'command_line_too_long'))
    elif kind == 'inline_diff_failed':
        def fail(*a, **kw):
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


def test_windows_lowercase(request_data, monkeypatch):
    monkeypatch.setattr(spec.runner, '_is_nt', lambda: True)
    monkeypatch.setattr(spec.runner, 'build_child_env', lambda *a, **kw: dict(
        pathext='.EXE', systemroot='system', comspec='cmd', temp=request_data['cwd']))
    request_data['dry_run'] = True
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    assert json.loads(out)['env']['pathext'] == '.EXE'


@pytest.mark.parametrize('error', [BrokenPipeError, OSError])
def test_response_write(request_data, error):
    class BrokenOutput:
        def write(self, output):
            raise error('private details')
    err = io.StringIO()
    assert spec.main(io.BytesIO(json.dumps(request_data).encode()), BrokenOutput(), err) == 1
    assert err.getvalue() == f'launch-spec failed: {error.__name__}\n'


def test_response_serialization(request_data, monkeypatch):
    monkeypatch.setattr(spec, 'build', lambda req: {'env': object()})
    assert invoke(request_data) == (1, '', 'launch-spec failed: TypeError\n')


@pytest.mark.parametrize('kind', ['leg_inactive', 'leg_role'])
def test_leg_eligibility(request_data, monkeypatch, kind):
    from dataclasses import replace
    leg = next(leg for leg in spec.DEFAULT_LEGS if leg.id == request_data['leg']['id'])
    if kind == 'leg_inactive':
        monkeypatch.setattr(spec, 'DEFAULT_LEGS', (replace(leg, disabled='policy'),))
        reason = f'leg disabled: {leg.id}: policy; override cannot enable a disabled leg'
    else:
        monkeypatch.setattr(spec, 'DEFAULT_LEGS', (replace(leg, disabled=None),))
        request_data['role'] = 'implement'
        reason = 'role implement not in leg roles'
    assert invoke(request_data) == (2, '', f'{kind}: {reason}\n')


def test_cwd_normalized(request_data, tmp_path):
    (tmp_path / 'sub').mkdir()
    request_data['cwd'] = str(tmp_path) + '/./sub/..'
    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    assert json.loads(out)['cwd'] == str(tmp_path)


def test_launch_spec_doc_status():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / 'docs' / 'SPAWNER.md').read_text()
    assert '(placement R6a-3d; nothing calls it yet)' in doc
    assert 'PLACEMENT=off' not in doc


@pytest.mark.parametrize('record', ['invalid_name', '{}', 'bad json', '[]', 'unreadable'])
@pytest.mark.parametrize('aged', [False, True])
def test_sweep_unusable(request_data, record, aged):
    from pathlib import Path
    root = Path(request_data['state'])
    name = 'run-x!' if record == 'invalid_name' else 'run-' + 'b' * 32
    path = root / 'launch' / name
    path.mkdir(parents=True)
    if record != 'invalid_name':
        target = root / 'runs' / name / 'record.json'
        target.parent.mkdir(parents=True)
        if record == 'unreadable':
            target.mkdir()  # Reading a directory raises OSError even as root.
        else:
            target.write_text(record)
    if aged:
        os.utime(path, (time.time() - 90000,) * 2)
    assert invoke(request_data)[0] == 0
    assert path.exists() is not aged


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
    request_data['leg'] = {'id': 'opencode-glm-5.3-flash-free'}
    code, out, err = invoke(request_data)
    assert code == 0 and json.loads(out)['env']['TEST_KEY'] == secret and secret not in err
    assert all(secret.encode() not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())


@pytest.mark.parametrize('unknown,expected', [(False, 0), (True, 2), ('bad', 1)])
def test_cli(request_data, monkeypatch, unknown, expected):
    from plangraph.spawner import cli
    if unknown is True:
        request_data['leg'] = {'id': 'unknown'}
    raw = b'bad' if unknown == 'bad' else json.dumps(request_data).encode()
    monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
    monkeypatch.setattr(cli.sys, 'stdout', io.StringIO())
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: pytest.fail('constructed before dispatch'))
    assert cli.main(['--state', 'ignored', 'launch-spec']) == expected
    if unknown == 'bad':
        assert cli.sys.stdout.getvalue() == ''
        assert cli.sys.stderr.getvalue() == 'launch-spec failed: invalid request (json)\n'


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


@pytest.mark.parametrize('writer', ['codex', 'cx-gpt-6.1-sol', 'gpt'])
def test_review_gateway_envelope_has_real_author(request_data, monkeypatch, writer):
    from plangraph.spawner import runner
    captured = []
    original = runner.build_child_env
    def capture(extra_env, *, headers):
        captured.append(headers)
        return original(extra_env, headers=headers)
    monkeypatch.setattr(runner, 'build_child_env', capture)
    request_data['writer_families'] = [writer]
    request_data['leg'] = {'id': 'omniroute-deepseek-flash'}
    code, out, err = invoke(request_data)
    assert code == 0, (out, err)
    assert captured[0]['X-OmniRoute-Author-Model'] == 'gpt'
    assert captured[0]['X-OmniRoute-Exclude-Family'] == 'gpt'
    assert captured[0]['X-OmniRoute-Task-Role'] == 'review'


@pytest.mark.parametrize('writers', [[], ['author'], ['unknown-writer'], ['unknown-' * 100]])
def test_launch_refuses_unresolved_writer(request_data, writers):
    request_data['writer_families'] = writers
    code, out, err = invoke(request_data)
    assert code == 2, (out, err)
    assert err.startswith('writer_family:')
    assert len(err) <= 301
    if writers:
        assert 'accepted names:' in err
        assert all(name in err for name in ('codex', 'gpt', 'deepseek', 'anthropic'))


def test_launch_refuses_unknown_review_of(request_data):
    request_data['writer_families'] = []
    request_data['review_of'] = 'no-such-run'
    code, out, err = invoke(request_data)
    assert code == 2 and out == '', (out, err)
    assert err.startswith('writer_family:') and 'unknown run id' in err


def test_launch_accepts_registered_beam_reviewer(request_data, go_credential):
    request_data['writer_families'] = ['codex']
    request_data['leg'] = {'id': 'opencode-go-beam-free'}
    code, out, err = invoke(request_data)
    assert code == 0, (out, err)
    assert 'opencode-go/beam-free' in json.loads(out)['argv']


def test_launch_refuses_same_family_deepseek(request_data):
    request_data['writer_families'] = ['deepseek']
    request_data['leg'] = {'id': 'omniroute-deepseek-flash'}
    code, out, err = invoke(request_data)
    assert code == 2, (out, err)
    assert 'review seat is a writer family' in err


def head_card(tmp_path, monkeypatch, writer_line):
    import subprocess
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    repo = tmp_path / 'headrepo'
    repo.mkdir()
    ident = ['-c', 'user.name=T', '-c', 'user.email=t@example.invalid']
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (repo / 'f').write_text('a\n')
    git('add', 'f')
    git(*ident, 'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (repo / 'f').write_text('b\n')
    git(*ident, 'commit', '-qam', f'change\n\nWriter: {writer_line}')
    return {'diff_ref': {'repo': str(repo), 'base': base, 'head': git('rev-parse', 'HEAD')}}


def test_launch_headers_carry_the_head_writer_trailer_family(request_data, monkeypatch, tmp_path):
    from plangraph.spawner import runner
    captured = []
    original = runner.build_child_env
    def capture(extra_env, *, headers):
        captured.append(headers)
        return original(extra_env, headers=headers)
    monkeypatch.setattr(runner, 'build_child_env', capture)
    request_data['card'] = head_card(tmp_path, monkeypatch, 'gemini-4-hydrogen-high run-1 (writer)')
    request_data['writer_families'] = ['openai']
    request_data['leg'] = {'id': 'omniroute-deepseek-flash'}
    code, out, err = invoke(request_data)
    assert code == 0, (out, err)
    assert set(captured[0]['X-OmniRoute-Exclude-Family'].split(',')) == {'gpt', 'gemini'}


def test_launch_works_with_only_the_head_trailer_and_refuses_its_own_family(request_data, monkeypatch, tmp_path):
    request_data['card'] = head_card(tmp_path, monkeypatch, 'deepseek run-1 (writer)')
    request_data['writer_families'] = []
    request_data['leg'] = {'id': 'omniroute-deepseek-flash'}
    code, out, err = invoke(request_data)
    assert code == 2 and 'review seat is a writer family' in err, (out, err)
    request_data['leg'] = {'id': 'qodercli-qwen3.8-flash'}
    code, out, err = invoke(request_data)
    assert code == 0, (out, err)


def test_launch_refuses_an_unreadable_head(request_data, monkeypatch, tmp_path):
    card = head_card(tmp_path, monkeypatch, 'gemini-4-hydrogen-high run-1 (writer)')
    card['diff_ref']['head'] = '0' * 40
    request_data['card'] = card
    code, out, err = invoke(request_data)
    assert code == 2 and err.startswith('writer_family:') and 'cannot read the Writer trailer' in err, (out, err)


def test_launch_accepts_a_registered_repository_diff_ref(request_data):
    request_data['card'] = {'diff_ref': {'repo_id': 'demo', 'base_sha': 'a' * 40, 'head_sha': 'b' * 40}}
    code, out, err = invoke(request_data)
    assert code == 0, (out, err)


@pytest.mark.parametrize('harness', ['opencode', 'codex'])
def test_review_launch_resolves_latest_nvm_harness_under_user_manager_path(
        request_data, tmp_path, monkeypatch, harness):
    import shutil
    from plangraph.planner.legs import REVIEW_GPT
    from plangraph.routing_support.clients import build_child_env

    manager_path = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', manager_path)
    monkeypatch.delenv('NVM_DIR', raising=False)
    monkeypatch.delenv('PLANGRAPH_CODEX_BIN', raising=False)
    monkeypatch.setattr(spec.runner, 'build_child_env', build_child_env)
    node = tmp_path / '.nvm/versions/node'
    for version in ('v9.0.0', 'v24.19.0'):
        bin_dir = node / version / 'bin'
        bin_dir.mkdir(parents=True)
        (bin_dir / harness).symlink_to('/bin/true')
    expected = str(node / 'v24.19.0/bin' / harness)
    request_data.update(leg={'id': REVIEW_GPT if harness == 'codex' else 'opencode-glm-5.3-flash-free'},
                        writer_families=['gemini'])

    code, out, err = invoke(request_data)
    assert (code, err) == (0, '')
    launch = json.loads(out)
    assert str(node / 'v24.19.0/bin') in launch['env']['PATH'].split(os.pathsep)
    assert launch['argv'][0] == expected
    assert shutil.which(harness, path=launch['env']['PATH']) == expected


def test_launch_spec_danger_full_access_precedes_client_lookup(request_data, monkeypatch):
    from plangraph.routing_support import clients
    def lookup(*args, **kwargs):
        raise AssertionError('client looked up before the sandbox refusal')
    monkeypatch.setattr(clients, 'codex_review_client', lookup)
    monkeypatch.setattr(spec, 'quota_paused', lookup, raising=False)
    request_data['card'] = {'sandbox': 'danger-full-access'}
    code, out, err = invoke(request_data)
    assert (code, out) == (2, '')
    assert err.startswith('sandbox_refused: danger-full-access')


def test_fresh_launch_spec_danger_full_access_precedes_run_tree(request_data, monkeypatch, capsys):
    from plangraph.spawner import git_guard
    def tree(*args, **kwargs):
        raise AssertionError('run tree leased before the sandbox refusal')
    monkeypatch.setattr(git_guard, 'run_tree', tree)
    request_data['card'] = {'sandbox': 'danger-full-access'}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(request_data)))
    assert spec.fresh_main() == 2
    captured = capsys.readouterr()
    assert captured.err.startswith('sandbox_refused: danger-full-access') and captured.out == ''


@pytest.mark.parametrize('card', [{'sandbox': 'unknown-sentinel-mode'}, {'sandbox': 7}])
def test_launch_spec_unsupported_sandbox_is_named_without_echo(request_data, card):
    request_data['card'] = card
    code, _, err = invoke(request_data)
    assert code == 2 and err.startswith('sandbox_refused: ') and 'sentinel' not in err
