"""The generated registry lists public repositories and keeps remote placement closed per host."""
import pytest

from plangraph.fleet_sync import genconfig
from plangraph.spawner import overflow


def host(name='worker', **extra):
    return dict(id=name, transport='ssh', os='linux', caps={'mem_floor_mb': 1},
                roles=['implement'], state_dir='/state', repos={
                    'plangraph': {'root': '/srv/pg'}, 'private-thing': {'root': '/srv/other'}},
                **extra)


def overflow_config(registry):
    return genconfig.generate(registry)['overflow-hosts.json']


def test_registry_without_public_repos_key_lists_this_public_repository():
    config = overflow_config({'self': 'worker', 'hosts': [dict(host(), transport='local')]})
    assert config['public_repos'] == ['/srv/pg']
    assert config['public_repo_ids'] == {'plangraph': '/srv/pg'}


@pytest.mark.parametrize('listed', [[], ['private-thing']])
def test_explicit_public_repos_wins_over_the_default(listed):
    config = overflow_config({'self': 'worker', 'public_repos': listed,
                              'hosts': [dict(host(), transport='local')]})
    assert config['public_repos'] == [{'private-thing': '/srv/other'}[k] for k in listed]


def test_remote_host_repos_follow_the_default_public_list():
    entry = overflow_config({'self': 'me', 'hosts': [host()]})['hosts']['worker']
    assert entry['repos'] == {'plangraph': '/srv/pg'}


@pytest.mark.parametrize('extra,open_', [({}, False), ({'remote_placement': False}, False),
                                         ({'remote_placement': 'yes'}, False),
                                         ({'remote_placement': True}, True)])
def test_generated_host_carries_a_switch_that_defaults_off(extra, open_):
    entry = overflow_config({'self': 'me', 'hosts': [host(**extra)]})['hosts']['worker']
    assert entry['remote_placement'] is open_


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'host_caps', lambda *a: (2048, 3000))
    monkeypatch.setattr(overflow, 'read_readiness', lambda name: dict(
        ok=True, live_runs=0, mem_available_mb=9000, disk_free_gb=50, fresh=True))


@pytest.mark.parametrize('extra,refused', [({}, True), ({'remote_placement': True}, False)])
def test_closed_switch_refuses_every_remote_candidate(fresh, extra, refused):
    config = overflow.generate({'self': 'me', 'hosts': [host(**extra)]})['overflow-hosts.json']
    config['registry'] = True
    row = overflow.ready_records(config)['worker']
    assert (row.get('refusal') == 'remote placement closed') is refused
    decision = overflow.decide(0, 2048, 3000, {'worker': row}, True)
    assert (decision.host is None) is refused


def test_hand_written_config_without_registry_flag_is_unaffected(fresh):
    config = {'hosts': {'worker': dict(ssh='worker')}, 'public_repos': []}
    assert 'refusal' not in overflow.ready_records(config)['worker']


def local_admission(tmp_path, monkeypatch, registry):
    """seat_spawn.admit for a review with a diff_ref on a registry host with room and no readiness file."""
    import json as _json
    from types import SimpleNamespace
    from plangraph.spawner import host_ready, seat_spawn
    config = tmp_path / 'config' / 'plangraph'
    config.mkdir(parents=True)
    (config / 'hosts.json').write_text(_json.dumps(registry))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config.parent))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_HOST_NAME', 'local')
    monkeypatch.delenv('PLANGRAPH_PLACED_HOST', raising=False)
    monkeypatch.setattr(seat_spawn, 'resources', lambda *a: (0, 30000))
    monkeypatch.setattr(host_ready, 'local_disk_admission', lambda *a, **k: (None, None))
    service = SimpleNamespace(state=tmp_path / 'state', runner=SimpleNamespace(default_cwd=str(tmp_path)))
    card = {'diff_ref': {'repo': str(tmp_path), 'base': 'abc', 'head': 'def'}}
    return seat_spawn.admit(service, {'role': 'review'}, card=card)


@pytest.mark.parametrize('public', [None, [], ['plangraph']])
def test_closed_remote_switches_keep_local_admission(tmp_path, monkeypatch, public):
    local = dict(host('local'), transport='local', roles=['implement', 'review'],
                 warmup={'ramp_mb': {'implement': 1024, 'review': 512}},
                 repos={'plangraph': {'root': str(tmp_path)}})
    worker = dict(host('worker'), roles=['implement', 'review'],
                  warmup={'ramp_mb': {'implement': 1024, 'review': 512}})
    registry = {'version': 1, 'self': 'local', 'hosts': [local, worker],
                **({} if public is None else {'public_repos': public})}
    # An omitted allowlist and an explicit empty one admit the same healthy local run.
    assert local_admission(tmp_path, monkeypatch, registry) is None


def native_review_admission(tmp_path, monkeypatch, *, pinned, remote_placement):
    """A native writer review card admitted on a registry host with one ready worker."""
    import json as _json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import patch
    from plangraph.spawner import host_ready, seat_spawn
    config = tmp_path / 'config' / 'plangraph'
    config.mkdir(parents=True)
    ready = tmp_path / '.local/state/plangraph/host-ready'
    ready.mkdir(parents=True)
    hosts = []
    for name, free in (('local', 8000), ('worker', 40000)):
        hosts.append(dict(host(name), transport='local' if name == 'local' else 'ssh',
                          roles=['implement', 'review'], placement_fallback=name == 'local',
                          warmup={'ramp_mb': {'implement': 1024, 'review': 512}},
                          repos={'plangraph': {'root': str(tmp_path) if name == 'local' else '/srv/pg'}},
                          **({'remote_placement': True} if name == 'worker' and remote_placement else {})))
        (ready / (name + '.json')).write_text(_json.dumps(dict(
            host=name, mem_available_mb=free, live_runs=0, disk_free_gb=50, ok=True,
            checked_at=datetime.now(timezone.utc).isoformat())))
    (config / 'hosts.json').write_text(_json.dumps(dict(version=1, self='local', hosts=hosts)))
    env = dict(XDG_CONFIG_HOME=str(config.parent), HOME=str(tmp_path), PLANGRAPH_HOST_NAME='local',
               **({'PLANGRAPH_PLACED_HOST': 'local'} if pinned else {}))
    card = {'review_workspace': True, 'repository_review_required': True,
            'native_writer_run': 'run-abc', 'item_key': 'demo:ITEM',
            'diff_ref': {'repo': str(tmp_path), 'base': 'abc', 'head': 'def'}}
    sent = []
    with patch.dict('os.environ', env), patch.object(seat_spawn, 'resources', return_value=(0, 8000)), \
            patch.object(host_ready, 'local_disk_admission', return_value=(None, None)), \
            patch.object(overflow, 'remote_spawn', lambda *a, **k: sent.append(a) or dict(
                run_id='run-r', status='running')):
        service = SimpleNamespace(state=tmp_path / 'state',
                                  runner=SimpleNamespace(default_cwd=str(tmp_path)))
        result = seat_spawn.admit(service, {'role': 'review'}, card=card)
    return result, sent


def test_native_review_enters_placement_without_the_writer_host_pin(tmp_path, monkeypatch):
    result, sent = native_review_admission(tmp_path, monkeypatch, pinned=False, remote_placement=True)
    assert [call[0] for call in sent] == ['worker'], result


def test_native_review_stays_local_while_every_switch_is_off(tmp_path, monkeypatch):
    result, sent = native_review_admission(tmp_path, monkeypatch, pinned=False, remote_placement=False)
    assert not sent and result is None


def test_the_writer_host_pin_keeps_the_review_local(tmp_path, monkeypatch):
    result, sent = native_review_admission(tmp_path, monkeypatch, pinned=True, remote_placement=True)
    assert not sent
