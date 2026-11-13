import json
import hashlib
import shlex
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from plangraph.spawner import overflow as o


def healthy(**changes):
    return dict(ssh='invented-alias', ok=True, fresh=True, live_runs=0,
                mem_available_mb=6000, role_mb=2048, floor_mb=3000, **changes)


@pytest.mark.parametrize('mem', [4000, 2999])
def test_best_host(mem):
    rows = {'birch': healthy(), 'ash': healthy(), 'cedar': healthy()}
    rows['cedar']['live_runs'] = 1
    assert o.decide(mem, 2048, 3000, rows, True).host == 'ash'


def test_local_room():
    assert o.decide(5048, 2048, 3000, {}, False) == (None, 'local room')


def bracket(command, receipt):
    markers = [word for word in shlex.split(command) if word.startswith('PLANGRAPH_RECEIPT_')]
    assert len(markers) == 2
    return markers[0] + '\n' + receipt + '\n' + markers[1] + '\n'


@pytest.mark.parametrize('field,value,reason', [('fresh', False, 'stale'), ('ok', False, 'failed'),
                                             ('ssh', None, 'no_route'), ('live_runs', None, 'malformed')])
def test_refused(field, value, reason):
    row = healthy()
    row[field] = value
    result = o.decide(3000, 2048, 3000, {'ash': row}, True)
    assert result.host is None and reason in result.reason


@pytest.mark.parametrize('field', ['live_runs', 'mem_available_mb'])
def test_negative_readiness_is_malformed(field):
    row = healthy()
    row[field] = -5
    result = o.decide(3000, 2048, 3000, {'first': row}, True)
    assert result.host is None and 'malformed readiness' in result.reason
    assert o.decide(3000, 2048, 3000, {'first': row, 'second': healthy()}, True).host == 'second'


@pytest.mark.parametrize('stdout,expected', [
    ('{"run_id":"run-r","status":"running","card":{"a":1}}',
     {'run_id': 'run-r', 'status': 'running', 'card': {'a': 1}}),
])
def test_remote_outer_receipt(monkeypatch, stdout, expected):
    def run(argv, **kwargs):
        output = '/invented-stage'
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            output = 'shell init\n{"status":"fake"}\n' + bracket(argv[-1], stdout) + '{"status":"later"}\n'
        return SimpleNamespace(returncode=0, stdout=output)
    monkeypatch.setattr(o.subprocess, 'run', run)
    host = dict(ssh='invented-alias', repo='/invented', spawner_src='/invented/src', env_file='/invented/env')
    assert o.remote_spawn('invented', host, {}, 'prompt', []) == expected


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    monkeypatch.delenv('PLANGRAPH_HOST_NAME', raising=False)
    registry = o.config_dir() / 'hosts.json'
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'self': 'local', 'hosts': [
        {'id': name, 'caps': {'max_runs': 4, 'mem_floor_mb': 3000},
         'warmup': {'ramp_mb': {'implement': 2048}}}
        for name in ('local', 'ash', 'birch')]}))
    config = tmp_path / 'config.json'
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    card = tmp_path / 'card.json'
    card.write_text(json.dumps({'diff_ref': {'repo': str(tmp_path), 'base': 'abc', 'head': 'def'}}))
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('a prompt')
    monkeypatch.setattr(o, 'local_resources', lambda state: (4, 4000))
    return tmp_path, config, ['spawn', '--state', str(tmp_path / 'state'), '--repo', str(tmp_path),
                              '--card', str(card), '--prompt', str(prompt)]


@pytest.mark.parametrize('config', [{}, {'public_repos': []}])
def test_private_never_copies(inputs, monkeypatch, capsys, config):
    root, path, args = inputs
    path.write_text(json.dumps(config))
    monkeypatch.setattr(o, 'remote_spawn', lambda *a: pytest.fail('private copy'))
    assert o.main(args) == 2
    assert 'not public' in capsys.readouterr().out


def test_local_cli_identical(inputs, monkeypatch):
    root, path, args = inputs
    monkeypatch.setattr(o, 'local_resources', lambda state: (0, 6000))
    calls = []
    monkeypatch.setattr(o.cli, 'main', lambda argv: calls.append(argv) or 3)
    assert o.main(args + ['--leg', 'invented-leg']) == 3
    assert calls == [['--state', str(root / 'state'), 'spawn', '--card', '@' + str(root / 'card.json'),
                      'a prompt', '--leg', 'invented-leg']]


def test_retry_and_queue_reasons(inputs, monkeypatch, capsys):
    root, path, args = inputs
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': {}, 'birch': {}}}))
    monkeypatch.setattr(o, 'ready_records', lambda config, role: {'ash': healthy(), 'birch': healthy()})
    calls = []
    def remote(name, *rest):
        calls.append(name)
        return None if name == 'ash' else {'status': 'running'}
    monkeypatch.setattr(o, 'remote_spawn', remote)
    assert o.main(args) == 0
    assert calls == ['ash', 'birch']
    assert json.loads(capsys.readouterr().out)['host'] == 'birch'
    monkeypatch.setattr(o, 'remote_spawn', lambda *a: None)
    assert o.main(args) == 2
    output = capsys.readouterr().out
    assert 'ash: remote spawn failed' in output and 'birch: remote spawn failed' in output
    record = json.loads(output)
    for field in ('overflow_reason', 'error'):
        assert record[field].count('ash: remote spawn failed') == 1
        assert record[field].count('birch: remote spawn failed') == 1


@pytest.mark.parametrize('contents', ['bad json', '{}', 'stale'])
def test_readiness_fail_closed(inputs, contents):
    root, _, _ = inputs
    directory = root / '.local/state/plangraph/host-ready'
    directory.mkdir(parents=True)
    if contents == 'stale':
        contents = json.dumps(dict(host='ash', ok=True, live_runs=0, mem_available_mb=6000,
                                   checked_at=(datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()))
    (directory / 'ash.json').write_text(contents)
    records = o.ready_records({'hosts': {'ash': {'ssh': 'invented-alias'}}})
    assert o.decide(3000, 2048, 3000, records, True).host is None
    if 'checked_at' in contents:
        assert 'stale' in o.decide(3000, 2048, 3000, records, True).reason


@pytest.mark.parametrize('card', ['{"diff_ref": {}}', '{"diff_ref": {"base": "abc"}}', None])
def test_local_before_card_parsing(inputs, monkeypatch, card):
    root, _, args = inputs
    if card is None:
        (root / 'card.json').unlink()
    else:
        (root / 'card.json').write_text(card)
    monkeypatch.setattr(o, 'local_resources', lambda state: (0, 6000))
    monkeypatch.setattr(o, 'ready_records', lambda config: pytest.fail('local readiness read'))
    monkeypatch.setattr(o.cli, 'main', lambda argv: 3)
    assert o.main(args) == 3


@pytest.mark.parametrize('receipt', [
    'hi\n{"status":"running"}\necho-trailing',
    'hi\n{\n  "status": "running"\n}\necho-trailing',
    'unreadable receipt',
])
def test_remote_env_echo_does_not_retry(inputs, monkeypatch, capsys, receipt):
    root, path, args = inputs
    host = dict(ssh='invented-alias', repo='/invented', spawner_src='/invented/src',
                env_file=str(root / 'env'))
    (root / 'env').write_text('echo hi\n')
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': host, 'birch': host}}))
    monkeypatch.setattr(o, 'ready_records', lambda config, role: {'ash': healthy(), 'birch': healthy()})
    spawns = []
    def run(argv, **kwargs):
        stdout = '/invented-stage\n'
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            spawns.append(argv)
            assert '. ' + str(root / 'env') in argv[-1]
            stdout = bracket(argv[-1], receipt)
        return SimpleNamespace(returncode=0, stdout=stdout)
    monkeypatch.setattr(o.subprocess, 'run', run)
    assert o.main(args) == 0
    assert len(spawns) == 1
    assert json.loads(capsys.readouterr().out)['host'] == 'ash'


def test_missing_memavailable_refuses(inputs, monkeypatch, capsys):
    _, _, args = inputs
    monkeypatch.setattr(o, 'local_resources', local_resources_real)
    service = o.cli.SpawnService()
    monkeypatch.setattr(service, 'ps', lambda: {'runs': []})
    monkeypatch.setattr(o.cli, 'SpawnService', lambda **kw: service)
    read_text = o.Path.read_text
    monkeypatch.setattr(o.Path, 'read_text', lambda self, *a, **kw:
                        'MemTotal: 4096 kB\n' if str(self) == '/proc/meminfo'
                        else read_text(self, *a, **kw))
    with pytest.raises(ValueError, match='MemAvailable'):
        o.local_resources()
    assert o.main(args) == 1
    assert 'resource check failed' in capsys.readouterr().err


@pytest.mark.parametrize('mode', ['missing_end', 'double', 'missing_begin', 'reversed', 'masked'])
def test_remote_receipt_boundary_and_mask(inputs, monkeypatch, capsys, mode):
    root, path, args = inputs
    host = dict(ssh='invented-alias', repo='/invented', spawner_src='/invented/src')
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': host}}))
    monkeypatch.setattr(o, 'ready_records', lambda config, role: {'ash': healthy()})
    secret = 'invented/' + 'credential123'
    spawns = []
    def run(argv, **kwargs):
        output = '/invented-stage'
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            spawns.append(argv)
            output = bracket(argv[-1], json.dumps({'run_id': 'run-r', 'status': 'running', 'note': 'password ' + secret}))
            lines = output.splitlines()
            if mode == 'missing_end':
                output = '\n'.join(lines[:-1])
            elif mode == 'missing_begin':
                output = '\n'.join(lines[1:])
            elif mode == 'double':
                output *= 2
            elif mode == 'reversed':
                output = '\n'.join(reversed(lines))
        return SimpleNamespace(returncode=0, stdout=output, stderr='password ' + secret)
    monkeypatch.setattr(o.subprocess, 'run', run)
    assert o.main(args) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    result = json.loads(captured.out)
    if mode == 'masked':
        assert result['note'] == 'password [redacted]'
    else:
        assert result['reason'] == 'receipt_unbracketed'
        assert result['status'] == 'unknown' and result['acknowledged_unknown']
    assert len(spawns) == 1


local_resources_real = o.local_resources


def test_local_real_cli_card_and_prompt(inputs, monkeypatch, capsys):
    root, _, args = inputs
    monkeypatch.setattr(o, 'local_resources', lambda state: (0, 6000))
    calls = []

    async def spawn(**payload):
        calls.append(payload)
        return {'status': 'dry_run', 'exit_code': 0}

    # Keep the real CLI parser and real service with temporary HOME/state;
    # intercept only execution so no model or external process is needed.
    service = o.cli.SpawnService()
    monkeypatch.setattr(service, 'spawn', spawn)
    monkeypatch.setattr(o.cli, 'SpawnService', lambda **kw: service)
    assert o.main(args + ['--leg', 'invented-leg', '--dry-run']) == 0
    assert calls[0]['card'] == json.loads((root / 'card.json').read_text())
    assert calls[0]['prompt'] == 'a prompt'
    assert calls[0]['leg'] == 'invented-leg'
    assert json.loads(capsys.readouterr().out)['status'] == 'dry_run'


def test_remote_rewrite_and_quoting(monkeypatch):
    calls, cards = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'scp':
            cards.append(json.loads(o.Path(argv[2]).read_text()))
        stdout = '/invented-stage\n'
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            stdout = bracket(argv[-1], '{"run_id":"run-r","status":"running"}')
        return SimpleNamespace(returncode=0, stdout=stdout)
    monkeypatch.setattr(o.subprocess, 'run', run)
    host = dict(ssh='invented-alias', repo='/invented checkout', spawner_src='/invented source', env_file='/invented env')
    card = {'diff_ref': dict(repo='/local-invented', base='abc', head='def')}
    assert o.remote_spawn('ash', host, card, 'secret prompt', ['--leg', 'a; nope'])['status'] == 'running'
    assert cards[0]['diff_ref']['repo'] == host['repo']
    assert card['diff_ref']['repo'] == '/local-invented'
    command = calls[-2][-1]
    assert "'a; nope'" in command and 'secret prompt' not in command
    assert 'git fetch origin -- abc' in command


def test_record_host(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_HOST_NAME', 'invented-local')
    service = o.cli.SpawnService(state=str(tmp_path))
    record = service.save_refusal(ValueError('invented refusal'), 'implement')
    assert record['host'] == 'invented-local'


@pytest.mark.parametrize('hosts,code', [({'invented': 'alias'}, 2), ([], 1), (None, 1)])
def test_malformed_host_config(inputs, monkeypatch, capsys, hosts, code):
    root, path, args = inputs
    path.write_text(json.dumps({'hosts': hosts, 'public_repos': [str(root)]}))
    monkeypatch.setattr(o, 'remote_spawn', lambda *a: pytest.fail('malformed host copied'))
    assert o.main(args) == code
    output = capsys.readouterr()
    if code == 2:
        assert 'refused:' in output.out and 'malformed readiness' in output.out
    else:
        assert 'input or resource check failed' in output.err


@pytest.mark.parametrize('key', ['local_run_cap', 'local_floor_mb'])
@pytest.mark.parametrize('value', ['4', 'invalid', None, [], {}])
def test_local_limits_coerce_or_default(inputs, monkeypatch, key, value):
    _, path, args = inputs
    path.write_text(json.dumps({key: value}))
    monkeypatch.setattr(o, 'local_resources', lambda state: (0, 6000))
    monkeypatch.setattr(o.cli, 'main', lambda argv: 3)
    assert o.main(args) == 3


@pytest.mark.parametrize('prompt', ['trailing\n', 'trailing\n\n', 'literal $(false) .\n', 'x' * (300 * 1024)],
                         ids=['newline', 'blank-line', 'shell-literal', '300KiB'])
def test_remote_prompt_bytes(tmp_path, monkeypatch, prompt):
    stage = tmp_path / 'stage with spaces'
    stage.mkdir()
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    policy = tmp_path / 'route-policy.json'
    policy.write_text('{"banned_families": [], "off_families": [], "paused_harnesses": {}}')
    env = dict(PATH=str(binary_dir), HOME=str(tmp_path), AUTOOS_ROOT=str(tmp_path),
               XDG_STATE_HOME=str(tmp_path / 'state'), PLANGRAPH_OMNIROUTE_LEG_STATE='0',
               PLANGRAPH_ROUTE_POLICY=str(policy))
    real_run = subprocess.run
    source = o.Path(__file__).resolve().parents[1] / 'src'
    # A source-only interpreter cannot fall back to an ambient site installation.
    (binary_dir / 'python3').write_text(
        f'#!{sys.executable} -S\n'
        'import os, runpy, sys\n'
        f'assert os.environ["PYTHONPATH"] == {str(source)!r}\n'
        'import plangraph.spawner.overflow as module\n'
        f'assert module.__file__ == {str(source / "plangraph/spawner/overflow.py")!r}\n'
        'assert sys.argv[1:3] == ["-m", "plangraph.spawner.cli"]\n'
        'sys.argv = sys.argv[2:]\n'
        'runpy.run_module("plangraph.spawner.cli", run_name="__main__")\n')
    (binary_dir / 'python3').chmod(0o700)
    received = []
    def run(argv, **kwargs):
        if argv[0] == 'scp':
            (stage / 'card.json').write_bytes(o.Path(argv[2]).read_bytes())
            (stage / 'prompt.txt').write_bytes(o.Path(argv[3]).read_bytes())
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            # Only SSH/SCP are faked: the unchanged shell runs the real CLI parser,
            # prompt-file reader, and dry-run launch path in an isolated environment.
            result = real_run(['/bin/sh', '-c', argv[-1]], capture_output=True, text=True,
                              env=env, timeout=30)
            assert result.returncode == 0, result.stderr
            received.append(json.loads('\n'.join(result.stdout.splitlines()[1:-1])))
            return result
        return SimpleNamespace(returncode=0, stdout=str(stage) if argv[-1] == 'mktemp -d' else '{}')
    monkeypatch.setattr(o.subprocess, 'run', run)
    host = dict(ssh='invented-alias', repo=str(tmp_path), spawner_src=str(source))
    options = ['--role', 'review', '--leg', 'agy-gemini-3.8-flash', '--writer-family', 'gpt',
               '--override-reason', 'hermetic prompt transport regression', '--dry-run', '--json']
    record = o.remote_spawn('invented', host, {'estimate_tokens': 1000}, prompt, options)
    assert received == [record]
    assert record['status'] == 'dry_run'
    assert record['prompt_sha256'] == hashlib.sha256(prompt.encode('utf-8')).hexdigest()


@pytest.mark.parametrize('receipt', ['unreadable', '{}', '[]', '{"status":"running"}'])
@pytest.mark.parametrize('entry', ['main', 'admit'])
def test_acknowledged_unknown_never_retries(inputs, monkeypatch, capsys, receipt, entry):
    from plangraph.spawner import seat_spawn as admission
    from plangraph.spawner.service import SpawnService
    root, path, args = inputs
    host = dict(ssh='invented', repo='/invented', spawner_src='/invented/src')
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': host, 'birch': host}}))
    monkeypatch.setattr(o, 'ready_records', lambda config, role='implement': {'ash': healthy(), 'birch': healthy()})
    spawns = []
    def run(argv, **kwargs):
        stdout = '/invented-stage'
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            spawns.append(argv)
            stdout = receipt if receipt == 'unreadable' else bracket(argv[-1], receipt)
        return SimpleNamespace(returncode=0, stdout=stdout)
    monkeypatch.setattr(o.subprocess, 'run', run)
    if entry == 'main':
        assert o.main(args) == 0
        record = json.loads(capsys.readouterr().out)
    else:
        service = SpawnService(state=str(root / 'state'))
        service.runner.default_cwd = str(root)
        monkeypatch.setattr(admission, 'resources', lambda service: (100, 4000))
        record = service.admit_spawn({}, card={})
        assert record in service.ps()['runs']
    assert record['status'] == 'unknown' and record['acknowledged_unknown']
    assert record['host'] == 'ash' and len(spawns) == 1
