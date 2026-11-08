import json
import re
import shlex
import subprocess
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from plangraph.spawner import overflow as o


def framed(command, payload):
    token = re.search(r'PLANGRAPH_RECEIPT_BEGIN_([a-f0-9]+)', command)[1]
    return ('{"status":"secret-before"}\nPLANGRAPH_RECEIPT_BEGIN_' + token + '\n' +
            payload + '\nPLANGRAPH_RECEIPT_END_' + token + '\n{"status":"secret-after"}')


@pytest.mark.parametrize('payload,reason', [
    ('{"status":"running"}', 'remote receipt missing run id'),
    ('not json', 'remote receipt invalid'),
])
def test_framed_receipt_refusal(monkeypatch, payload, reason):
    def run(argv, **kwargs):
        stdout = '/stage'
        if 'plangraph.spawner.cli' in argv[-1]:
            stdout = framed(argv[-1], payload)
        return SimpleNamespace(returncode=0, stdout=stdout)
    monkeypatch.setattr(o.subprocess, 'run', run)
    with pytest.raises(o.RemoteRefusal, match=reason):
        o.remote_spawn('host', dict(ssh='stub', repo='/repo', spawner_src='/src'), {}, '', [])


def test_only_sentinel_receipt_printed(inputs, monkeypatch, capsys):
    root, path, args = inputs
    host = dict(ssh='stub', repo='/repo', spawner_src='/src', env_file='/env')
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': host}}))
    monkeypatch.setattr(o, 'ready_records', lambda config: {'ash': healthy()})
    def run(argv, **kwargs):
        stdout = '/stage'
        if 'plangraph.spawner.cli' in argv[-1]:
            stdout = framed(argv[-1], '{"run_id":"run-real","status":"running"}')
        return SimpleNamespace(returncode=0, stdout=stdout)
    monkeypatch.setattr(o.subprocess, 'run', run)
    assert o.main(args) == 0
    output = capsys.readouterr()
    assert 'secret' not in output.out + output.err
    assert json.loads(output.out)['run_id'] == 'run-real'


def healthy(**changes):
    return dict(ssh='invented-alias', ok=True, fresh=True, live_runs=0,
                mem_available_mb=6000, run_cap=4, floor_mb=3000, **changes)


@pytest.mark.parametrize('live,mem', [(4, 9000), (0, 2999)])
def test_best_host(live, mem):
    rows = {'birch': healthy(), 'ash': healthy(), 'cedar': healthy()}
    rows['cedar']['live_runs'] = 1
    assert o.decide(live, mem, 4, 3000, rows, True).host == 'ash'


def test_local_room():
    assert o.decide(3, 3000, 4, 3000, {}, False) == (None, 'local room')


@pytest.mark.parametrize('field,value,reason', [('fresh', False, 'stale'), ('ok', False, 'failed'),
                                             ('ssh', None, 'no_route'), ('live_runs', None, 'malformed')])
def test_refused(field, value, reason):
    row = healthy()
    row[field] = value
    result = o.decide(4, 6000, 4, 3000, {'ash': row}, True)
    assert result.host is None and reason in result.reason


@pytest.mark.parametrize('field', ['live_runs', 'mem_available_mb'])
def test_negative_readiness_is_malformed(field):
    row = healthy()
    row[field] = -5
    result = o.decide(4, 6000, 4, 3000, {'first': row}, True)
    assert result.host is None and 'malformed readiness' in result.reason
    assert o.decide(4, 6000, 4, 3000, {'first': row, 'second': healthy()}, True).host == 'second'


@pytest.mark.parametrize('stdout,expected', [
    ('{"status":"running","card":{"a":1}}', {'status': 'running', 'card': {'a': 1}}),
    ('{"env":"secret"}\n{"status":"running","card":{"a":1}}\nnoise',
     {'status': 'running', 'card': {'a': 1}}),
])
def test_remote_outer_receipt(monkeypatch, stdout, expected):
    def run(argv, **kwargs):
        output = '/invented-stage'
        if 'plangraph.spawner.cli' in argv[-1]:
            output = framed(argv[-1], json.dumps(dict(expected, run_id='run-remote')))
        return SimpleNamespace(returncode=0, stdout=output)
    monkeypatch.setattr(o.subprocess, 'run', run)
    host = dict(ssh='invented-alias', repo='/invented', spawner_src='/invented/src', env_file='/invented/env')
    assert o.remote_spawn('invented', host, {}, 'prompt', []) == dict(expected, run_id='run-remote')


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = tmp_path / 'config.json'
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    card = tmp_path / 'card.json'
    card.write_text(json.dumps({'diff_ref': {'repo': str(tmp_path), 'base': 'abc', 'head': 'def'}}))
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('a prompt')
    monkeypatch.setattr(o, 'local_resources', lambda: (4, 6000))
    return tmp_path, config, ['spawn', '--repo', str(tmp_path), '--card', str(card), '--prompt', str(prompt)]


@pytest.mark.parametrize('config', [{}, {'public_repos': []}])
def test_private_never_copies(inputs, monkeypatch, capsys, config):
    root, path, args = inputs
    path.write_text(json.dumps(config))
    monkeypatch.setattr(o, 'remote_spawn', lambda *a: pytest.fail('private copy'))
    assert o.main(args) == 5
    assert 'not public' in capsys.readouterr().out


def test_local_cli_identical(inputs, monkeypatch):
    root, path, args = inputs
    monkeypatch.setattr(o, 'local_resources', lambda: (0, 6000))
    calls = []
    monkeypatch.setattr(o.cli, 'main', lambda argv: calls.append(argv) or 3)
    assert o.main(args + ['--leg', 'invented-leg']) == 3
    assert calls == [['spawn', '--card', '@' + str(root / 'card.json'), 'a prompt', '--leg', 'invented-leg']]


def test_retry_and_queue_reasons(inputs, monkeypatch, capsys):
    root, path, args = inputs
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': {}, 'birch': {}}}))
    monkeypatch.setattr(o, 'ready_records', lambda config: {'ash': healthy(), 'birch': healthy()})
    calls = []
    def remote(name, *rest):
        calls.append(name)
        return None if name == 'ash' else {'status': 'running'}
    monkeypatch.setattr(o, 'remote_spawn', remote)
    assert o.main(args) == 0
    assert calls == ['ash', 'birch']
    assert json.loads(capsys.readouterr().out)['host'] == 'birch'
    monkeypatch.setattr(o, 'remote_spawn', lambda *a: None)
    assert o.main(args) == 5
    output = capsys.readouterr().out
    assert 'ash: remote spawn failed' in output and 'birch: remote spawn failed' in output


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
    assert o.decide(4, 6000, 4, 3000, records, True).host is None
    if 'checked_at' in contents:
        assert 'stale' in o.decide(4, 6000, 4, 3000, records, True).reason


@pytest.mark.parametrize('card', ['{"diff_ref": {}}', '{"diff_ref": {"base": "abc"}}', None])
def test_local_before_card_parsing(inputs, monkeypatch, card):
    root, _, args = inputs
    if card is None:
        (root / 'card.json').unlink()
    else:
        (root / 'card.json').write_text(card)
    monkeypatch.setattr(o, 'local_resources', lambda: (0, 6000))
    monkeypatch.setattr(o, 'ready_records', lambda config: pytest.fail('local readiness read'))
    monkeypatch.setattr(o.cli, 'main', lambda argv: 3)
    assert o.main(args) == 3


@pytest.mark.parametrize('receipt', [
    'hi\n{"status":"running"}\necho-trailing',
    'hi\n{\n  "status": "running"\n}\necho-trailing',
])
def test_remote_env_echo_does_not_retry(inputs, monkeypatch, capsys, receipt):
    root, path, args = inputs
    host = dict(ssh='invented-alias', repo='/invented', spawner_src='/invented/src',
                env_file=str(root / 'env'))
    (root / 'env').write_text('echo hi\n')
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': host, 'birch': host}}))
    monkeypatch.setattr(o, 'ready_records', lambda config: {'ash': healthy(), 'birch': healthy()})
    spawns = []
    def run(argv, **kwargs):
        stdout = '/invented-stage\n'
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            spawns.append(argv)
            assert '. ' + str(root / 'env') in argv[-1]
            stdout = framed(argv[-1], '{"status":"running","run_id":"run-remote"}')
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


local_resources_real = o.local_resources


def test_local_real_cli_card_and_prompt(inputs, monkeypatch, capsys):
    root, _, args = inputs
    monkeypatch.setattr(o, 'local_resources', lambda: (0, 6000))
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
        if 'plangraph.spawner.cli' in argv[-1]:
            stdout = framed(argv[-1], '{"status":"running","run_id":"run-remote"}')
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


@pytest.mark.parametrize('hosts,code', [({'invented': 'alias'}, 5), ([], 1), (None, 1)])
def test_malformed_host_config(inputs, monkeypatch, capsys, hosts, code):
    root, path, args = inputs
    path.write_text(json.dumps({'hosts': hosts, 'public_repos': [str(root)]}))
    monkeypatch.setattr(o, 'remote_spawn', lambda *a: pytest.fail('malformed host copied'))
    assert o.main(args) == code
    output = capsys.readouterr()
    if code == 5:
        assert 'queue:' in output.out and 'malformed readiness' in output.out
    else:
        assert 'input or resource check failed' in output.err


@pytest.mark.parametrize('key', ['local_run_cap', 'local_floor_mb'])
@pytest.mark.parametrize('value', ['4', 'invalid', None, [], {}])
def test_local_limits_coerce_or_default(inputs, monkeypatch, key, value):
    _, path, args = inputs
    path.write_text(json.dumps({key: value}))
    monkeypatch.setattr(o, 'local_resources', lambda: (0, 6000))
    monkeypatch.setattr(o.cli, 'main', lambda argv: 3)
    assert o.main(args) == 3


@pytest.mark.parametrize('prompt', ['trailing\n', 'trailing\n\n', 'literal $(false) .\n'])
def test_remote_prompt_bytes(tmp_path, monkeypatch, prompt):
    stage = tmp_path / 'stage with spaces'
    stage.mkdir()
    real_run = subprocess.run
    received = []
    def run(argv, **kwargs):
        if argv[0] == 'scp':
            (stage / 'prompt.txt').write_bytes(o.Path(argv[3]).read_bytes())
        if argv[0] == 'ssh' and 'plangraph.spawner.cli' in argv[-1]:
            # Execute the actual generated shell, replacing only its spawn command.
            command = argv[-1].replace('python3 -m plangraph.spawner.cli spawn',
                                      shlex.join(['python3', '-c',
                                                  'import sys; print(repr(sys.argv[3]))']))
            result = real_run(['sh', '-c', command], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            received.append(result.stdout.splitlines()[1])
            return SimpleNamespace(returncode=0, stdout=framed(argv[-1], '{"run_id":"run-remote"}'))
        return SimpleNamespace(returncode=0, stdout=str(stage))
    monkeypatch.setattr(o.subprocess, 'run', run)
    host = dict(ssh='invented-alias', repo=str(tmp_path), spawner_src='src')
    assert o.remote_spawn('invented', host, {}, prompt, []) == {'run_id': 'run-remote'}
    assert received == [repr(prompt)]
