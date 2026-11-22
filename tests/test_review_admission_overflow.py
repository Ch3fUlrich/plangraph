"""A review without local room is placed on the next admitting host or refused at once with the reason (LE-REVIEW-RUNS-QUEUE-ON-CODING-VM-DEFAULT-CAPS)."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from registry_route_util import use_projected_overflow_config

from plangraph.spawner import overflow, seat_spawn

REGISTRY = {'self': 'home-host', 'hosts': [
    {'id': 'home-host', 'caps': {'mem_floor_mb': 3000},
     'warmup': {'ramp_mb': {'implement': 2048, 'review': 512, 'research': 512}}},
    {'id': 'build-host', 'caps': {'mem_floor_mb': 4096}, 'warmup': {'ramp_mb': {'implement': 256, 'review': 256}}}]}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_HOST_NAME', 'home-host')
    monkeypatch.setattr(overflow, 'registry_caps', lambda: REGISTRY)
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path)), _save=Mock(), state=tmp_path)
    return service, tmp_path


def config(tmp_path, monkeypatch, hosts=True):
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps({'public_repos': [str(tmp_path)],
                                'hosts': {'build-host': {'ssh': 'build-host'}} if hosts else {}}))
    use_projected_overflow_config(monkeypatch, path)


def test_local_limits_use_the_review_ramp_of_the_host():
    assert overflow.local_limits(REGISTRY, 'review') == (512, 3000)
    assert overflow.local_limits(REGISTRY, 'implement') == (2048, 3000)


def test_review_fits_locally_where_an_implement_run_would_not(host, monkeypatch):
    service, tmp_path = host
    config(tmp_path, monkeypatch)
    monkeypatch.setattr(seat_spawn, 'resources', lambda svc: (0, 4624))   # the 2026-11-20 reading
    assert seat_spawn.admit(service, {'role': 'review'}) is None
    refused = seat_spawn.admit(service, {'role': 'implement'})
    assert refused['status'] == 'refused'


def test_review_refused_locally_is_placed_on_a_ready_overflow_host(host, monkeypatch):
    service, tmp_path = host
    config(tmp_path, monkeypatch)
    monkeypatch.setattr(seat_spawn, 'resources', lambda svc: (0, 3300))   # 3300 - 512 < 3000
    monkeypatch.setattr(overflow, 'ready_records', lambda cfg, role: {'build-host': dict(
        ssh='build-host', ok=True, fresh=True, live_runs=1, mem_available_mb=12000, role_mb=256, floor_mb=4096)})
    remote = Mock(return_value={'run_id': 'run-remote', 'status': 'running'})
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    record = seat_spawn.admit(service, {'role': 'review'}, card={'diff_ref': {'repo': str(tmp_path)}})
    assert record['host'] == 'build-host' and record['overflow_remote']
    remote.assert_called_once()


def test_unplaceable_review_is_refused_not_queued_with_the_reason(host, monkeypatch):
    service, tmp_path = host
    config(tmp_path, monkeypatch, hosts=False)
    monkeypatch.setattr(seat_spawn, 'resources', lambda svc: (0, 3300))
    record = seat_spawn.admit(service, {'role': 'review'}, card={'diff_ref': {'repo': str(tmp_path)}})
    assert record['status'] == 'refused' and record['exit_code'] == 2
    assert record['reason'].startswith('memory: free 3300 MB, floor 3000 MB, role 512 MB')
    assert 'genconfig write' in record['hint']


def test_absent_overflow_config_refuses_with_a_hint(host, monkeypatch):
    service, tmp_path = host
    use_projected_overflow_config(monkeypatch, tmp_path / 'absent.json')
    monkeypatch.setattr(seat_spawn, 'resources', lambda svc: (0, 3300))
    record = seat_spawn.admit(service, {'role': 'review'})
    assert record['status'] == 'refused' and 'genconfig write' in record['hint']


@pytest.mark.parametrize('state, refusal', [
    ('{"phase":"ready"}', None),
    ('{"phase":"unreachable"}', 'worker host unreachable'),
    ('{"phase":"rejoining"}', 'worker host rejoining'),
    ('{invalid', 'worker host state unavailable'),
    (None, None),
])
@pytest.mark.parametrize('entry', ['service', 'overflow-cli'])
def test_placement_reads_worker_phase(host, monkeypatch, capsys, state, refusal, entry):
    service, root = host
    config(root, monkeypatch)
    monkeypatch.setattr(seat_spawn, 'resources', lambda svc: (0, 3300))
    monkeypatch.setattr(overflow, 'local_resources', lambda state: (0, 3300))
    monkeypatch.setattr(overflow, 'ready_records', lambda cfg, role: {'build-host': dict(
        ssh='build-host', ok=True, fresh=True, live_runs=1, mem_available_mb=12000, role_mb=256, floor_mb=4096)})
    remote = Mock(return_value={'run_id': 'run-remote', 'status': 'running'})
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    directory = root / 'worker-hosts'
    directory.mkdir()
    if state is not None:
        (directory / 'build-host.json').write_text(state)
    if entry == 'service':
        result = seat_spawn.admit(service, {'role': 'review'}, card={'diff_ref': {'repo': str(root)}})
    else:
        card, prompt = root / 'card.json', root / 'prompt'
        card.write_text('{}')
        prompt.write_text('fixture')
        monkeypatch.setattr(overflow.cli, 'SpawnService', lambda **kw: service)
        code = overflow.main(['spawn', '--state', str(root), '--repo', str(root),
                              '--card', str(card), '--prompt', str(prompt)])
        assert code == (2 if refusal else 0)
        result = json.loads(capsys.readouterr().out)
    if refusal:
        assert result['status'] == 'refused' and refusal in result['overflow_reason']
        remote.assert_not_called()
    else:
        assert result['host'] == 'build-host' and result['status'] == 'running'
        remote.assert_called_once()
