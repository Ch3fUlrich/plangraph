"""Standalone overflow refusal detail and invoking-state regressions."""
import json
from unittest.mock import Mock

import pytest

from plangraph.spawner import overflow as o
from plangraph.spawner.service import SpawnService
from test_spawner_overflow import healthy, inputs


@pytest.mark.parametrize('orchestration', [False, True])
def test_overflow_local_cli_preserves_caller_state(inputs, monkeypatch, capsys, orchestration):
    root, _, args = inputs
    monkeypatch.setattr(o, 'local_resources', lambda state: (0, 9000))
    monkeypatch.setattr(o, 'placement_policy', lambda *a: ('invented-local', orchestration))
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(root / 'default-state'))
    monkeypatch.setattr(o.cli, 'SpawnService', SpawnService)

    async def spawn(service, **options):
        assert service.state == root / 'state'
        assert options['card'] == json.loads((root / 'card.json').read_text())
        assert options['prompt'] == 'a prompt'
        record = dict(run_id='run-local', status='dry_run', exit_code=0, ended=1001)
        service._save(record)
        return record

    monkeypatch.setattr(SpawnService, 'spawn', spawn)
    assert o.main(args + ['--dry-run', '--leg', 'invented']) == 0
    record = json.loads(capsys.readouterr().out)
    assert SpawnService(state=root / 'state').result(record['run_id']) == record
    assert not (root / 'default-state').exists()


def test_overflow_requires_caller_state(inputs, monkeypatch, capsys):
    root, _, args = inputs
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(root / 'default-state'))
    index = args.index('--state')
    with pytest.raises(SystemExit) as exc:
        o.main(args[:index] + args[index + 2:])
    assert exc.value.code == 2 and '--state' in capsys.readouterr().err
    assert not (root / 'default-state').exists()


def test_overflow_remote_does_not_forward_local_state(inputs, monkeypatch, capsys):
    root, path, args = inputs
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': {}}}))
    monkeypatch.setattr(o, 'ready_records', lambda *a: {'ash': healthy()})
    remote = Mock(return_value=dict(run_id='run-remote', status='running'))
    monkeypatch.setattr(o, 'remote_spawn', remote)
    assert o.main(args + ['--role', 'review']) == 0
    assert remote.call_args.args[-1] == ['--role', 'review']
    assert json.loads(capsys.readouterr().out)['host'] == 'ash'


@pytest.mark.parametrize('detail', ['private repo', 'malformed hosts', 'refused: retry limit'])
def test_nonmemory_refusal_reason_and_caller_state(inputs, monkeypatch, capsys, detail):
    root, path, args = inputs
    path.write_text('{}')
    monkeypatch.setattr(o, 'decide', lambda *a: o.Decision(None, detail))
    monkeypatch.setattr(o, 'memory_refusal', lambda *a: None)
    monkeypatch.setattr(o, 'ready_records', lambda *a: {
        'ash': {'refusal': 'remote spawn failed'}, 'birch': {'refusal': 'remote copy failed'}})
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(root / 'default-state'))
    assert o.main(args) == 2
    output = capsys.readouterr()
    record = json.loads(output.out)
    expected = detail + '; ash: remote spawn failed; birch: remote copy failed'
    assert record['reason'] == record['error'] == record['overflow_reason'] == expected
    assert output.err.strip() == expected
    assert 'host_memory_floor' not in output.out + output.err
    assert SpawnService(state=root / 'state').result(record['run_id']) == record
    assert not (root / 'default-state').exists()


def test_overflow_retry_limit_preserves_host_refusals(inputs, monkeypatch, capsys):
    root, path, args = inputs
    names = ('ash', 'birch', 'cedar')
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': dict.fromkeys(names, {})}))
    monkeypatch.setattr(o, 'ready_records', lambda *a: {name: healthy() for name in names})
    remote = Mock(side_effect=[None, o.RemoteRefusal('remote copy failed')])
    monkeypatch.setattr(o, 'remote_spawn', remote)
    assert o.main(args) == 2
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record['status'] == 'refused' and record['reason'] == 'host_memory_floor'
    assert record['exit_code'] == 2 and record['ended'] is not None
    assert (record['mem_available_mb'], record['floor_mb'], record['role_mb']) == (4000, 3000, 2048)
    for text in ('refused: retry limit', 'ash: remote spawn failed', 'birch: remote copy failed'):
        assert text in record['overflow_reason'] and text in record['error'] and text in output.err
    assert [call.args[0] for call in remote.call_args_list] == ['ash', 'birch']
    assert SpawnService(state=root / 'state').result(record['run_id']) == record
