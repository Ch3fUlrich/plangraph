import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from plangraph.spawner import overflow as o


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


def test_remote_env_echo_does_not_retry(inputs, monkeypatch, capsys):
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
            stdout = 'hi\n{"status":"running"}\n\n'
        return SimpleNamespace(returncode=0, stdout=stdout)
    monkeypatch.setattr(o.subprocess, 'run', run)
    assert o.main(args) == 0
    assert len(spawns) == 1
    assert json.loads(capsys.readouterr().out)['host'] == 'ash'


def test_remote_rewrite_and_quoting(monkeypatch):
    calls, cards = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'scp':
            cards.append(json.loads(o.Path(argv[2]).read_text()))
        stdout = '/invented-stage\n' if argv[-1] == 'mktemp -d' else '{"status":"running"}'
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
